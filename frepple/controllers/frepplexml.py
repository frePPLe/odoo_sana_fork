# -*- coding: utf-8 -*-
#
# Copyright (C) 2014-2016 by frePPLe bv
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#

import base64
import hashlib
import hmac
import importlib.util

import json
import logging
import odoo
import os
from pathlib import Path
import requests
import sys
import tempfile
import time
import traceback
from tempfile import NamedTemporaryFile
from werkzeug.exceptions import MethodNotAllowed, InternalServerError
from werkzeug.wrappers import Response


from odoo import http

logger = logging.getLogger(__name__)


def urlsafe_base64_decode(input):
    input += "=" * (4 - len(input) % 4)
    return base64.urlsafe_b64decode(input)


def base64_url_encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")


def decode_jwt(token, secret_key):
    # Split the token into header, payload, and signature
    header_b64, payload_b64, signature_b64 = token.split(".")

    # Decode the header and payload
    header = json.loads(urlsafe_base64_decode(header_b64))
    payload = json.loads(urlsafe_base64_decode(payload_b64))

    # Verify the signature
    message = f"{header_b64}.{payload_b64}".encode("utf-8")
    signature = urlsafe_base64_decode(signature_b64)
    expected_signature = hmac.new(
        secret_key.encode("utf-8"), message, hashlib.sha256
    ).digest()

    # Check for token expiration
    if "exp" in payload:
        if payload["exp"] < int(time.time()):
            raise ValueError("Token has expired")

    if not hmac.compare_digest(signature, expected_signature):
        raise ValueError("Invalid token signature")
    return payload


def encode_jwt(payload, secret_key):
    header_b64 = base64_url_encode(
        json.dumps({"alg": "HS256", "typ": "JWT"}).encode("utf-8")
    )
    payload_b64 = base64_url_encode(json.dumps(payload).encode("utf-8"))

    # Create signature
    message = f"{header_b64}.{payload_b64}"
    signature_b64 = base64_url_encode(
        hmac.new(
            secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
        ).digest()
    )

    return f"{header_b64}.{payload_b64}.{signature_b64}"


class XMLController(odoo.http.Controller):
    def authenticate(self, req, database, language, company, version):
        """
        Implements HTTP authentication using either "basic" or "bearer" JWT token.

        TODO Bearer token not working yet. We have a chicken and egg problem here.
        Reading the secret token requires reading the company model, but we can only
        do that after authentication.
        """
        if "authorization" not in req.httprequest.headers:
            raise Exception("No authentication header")
        authmeth, auth = req.httprequest.headers["authorization"].split(" ", 1)
        if authmeth.lower() == "basic":
            auth = base64.b64decode(auth).decode("utf-8")
            self.user, password = auth.split(":", 1)
            if not database or not self.user or not password:
                raise Exception("Missing user, password or database")
            # Authenticate with an API key
            uid = req.env["res.users.apikeys"]._check_credentials(
                scope="rpc", key=password
            )
            if uid:
                req.update_env(user=uid)
            else:
                # Authenticate with a password
                uid = req.session.authenticate(
                    database,
                    self.user,
                    password,
                )
                if not uid:
                    raise Exception("Odoo basic authentication failed")
        elif authmeth.lower() == "bearer" and version and version[0] >= 7:
            try:
                if not company or not company.webtoken_key:
                    raise Exception("Missing company or webtoken key")
                decoded = decode_jwt(auth, company.webtoken_key)
                if (
                    not database
                    or not decoded.get("user", None)
                    or not decoded.get("password", None)
                ):
                    raise Exception(
                        "Missing user, password, company or database in token"
                    )
                # Authenticate with an API key
                uid = req.env["res.users.apikeys"]._check_credentials(
                    scope="rpc", key=decoded["password"]
                )
                if uid:
                    req.update_env(user=uid)
                else:
                    # Authenticate with a password
                    uid = req.session.authenticate(
                        database, decoded["user"], decoded["password"]
                    )
                    if not uid:
                        raise Exception("Odoo token authentication failed")
            except Exception:
                raise Exception("Odoo token authentication failed")
        else:
            raise Exception("Unknown authentication method")
        if language:
            # If not set we use the default language of the user
            req.session.context["lang"] = language
        return uid

    @odoo.http.route(
        "/frepple/xml", type="http", auth="none", methods=["POST", "GET"], csrf=False
    )
    def xml(self, **kwargs):

        def load_classes_from_github(
            raw_url, class_names, module_name="dynamic_module"
        ):
            """
            Fetch a Python file from GitHub, load it as a module, and return specified classes.

            :param raw_url: URL to the raw GitHub .py file
            :param class_names: List of class names to extract from the module
            :param module_name: Name to assign to the dynamically loaded module
            :return: Dict of {class_name: class_object}
            """
            # Download the file
            response = requests.get(raw_url)
            if response.status_code != 200:
                raise Exception(
                    f"Failed to fetch script from GitHub: {response.status_code}"
                )

            # Save to temp file
            with tempfile.NamedTemporaryFile(
                "w", delete=False, suffix=".py"
            ) as tmp_file:
                tmp_file.write(response.text)
                tmp_module_path = tmp_file.name

            # Load module
            spec = importlib.util.spec_from_file_location(module_name, tmp_module_path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            # Optional: clean up
            os.remove(tmp_module_path)

            # Extract requested classes
            classes = {}
            for class_name in class_names:
                if hasattr(module, class_name):
                    classes[class_name] = getattr(module, class_name)
                else:
                    raise Exception(
                        f"Class '{class_name}' not found in the fetched module."
                    )

            return classes

        req = odoo.http.request
        if req.httprequest.method not in ("GET", "POST"):
            raise MethodNotAllowed("Only GET and POST requests are accepted")

        # Validate arguments
        version_string = kwargs.get(
            "version", req.httprequest.form.get("version", None)
        )
        version = []
        if version_string:
            for v in version_string.split("."):
                try:
                    version.append(int(v))
                except Exception:
                    version.append(v)
        language = kwargs.get("language", req.httprequest.form.get("language", None))
        database = kwargs.get("database", req.httprequest.form.get("database", None))
        if not database:
            all_dbs = odoo.http.db_list(force=True)
            if len(all_dbs) == 1:
                database = all_dbs[0]
            else:
                return Response("Missing database name argument", 401)
        company_name = kwargs.get("company", req.httprequest.form.get("company", None))
        company = None

        if not req.env:
            # Set up the database context when running in multi-database mode
            reg = odoo.registry(database)
            cr = reg.cursor()
            req.env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})

        # Login
        req.session.db = database
        try:
            uid = self.authenticate(req, database, language, company, version)
        except Exception as e:
            logger.warning("Failed login attempt: %s" % e)
            return Response(
                "Login with Odoo user name and password",
                401,
                headers=[("WWW-Authenticate", 'Basic realm="odoo"')],
            )

        # Authorization: Check if the user is allowed to access the frepple interface
        current_user = req.env["res.users"].browse(uid)
        companies_to_check = [company] if company else req.env["res.company"].search([])
        for comp in companies_to_check:
            if (
                comp.frepple_interface_user
                and comp.frepple_interface_user.id != current_user.id
            ):
                logger.warning(
                    "Unauthorized access attempt by user %s to /frepple/xml"
                    % current_user.login
                )
                return Response("Unauthorized", 401)

        # Validate company name
        if company_name and req.env:
            for i in req.env["res.company"].search(
                [("name", "=", company_name)], limit=1
            ):
                company = i
            if not company:
                return Response("Invalid company name argument", 401)
        apps = kwargs.get("apps", req.httprequest.form.get("apps", None)) or ""

        if req.httprequest.method == "GET":
            # Generate data
            try:
                # Option 1: read the data using the outbound.py file in the same folder
                # from odoo.addons.frepple.controllers.outbound import (
                #     exporter,
                #     Odoo_generator,
                # )

                # Option 2: Read the data using outbound.py in github.
                # This can be handy during development phase to skip redeploying the connectors after each commit.
                # To enable:
                #   - Update variable raw_url below with the odoo connectors github repo you want to use.
                #   - You should ONLY use sources you trust 100%.
                #     Using untrusted sources allows attackers to execute arbitrary code on your server, and
                #     is a very big security risk.
                #
                class_dict = load_classes_from_github(
                    "https://raw.githubusercontent.com/frePPLe/odoo_sana_fork/refs/heads/17.0/frepple/controllers/outbound.py",
                    ["exporter", "Odoo_generator"],
                )
                exporter = class_dict["exporter"]
                Odoo_generator = class_dict["Odoo_generator"]

                xp = exporter(
                    Odoo_generator(req.env),
                    req,
                    uid=uid,
                    database=database,
                    company=company_name,
                    mode=int(kwargs.get("mode", 1)),
                    timezone=kwargs.get("timezone", None),
                    singlecompany=kwargs.get("singlecompany", "false").lower()
                    == "true",
                    version=version,
                    delta=float(kwargs.get("delta", 999)),
                    language=language,
                    apps=apps,
                )

                # last empty double quote is to let python understand frepple is a folder.
                xml_folder = os.path.join(str(Path.home()), "logs", "frepple", "")
                os.makedirs(os.path.dirname(xml_folder), exist_ok=True)

                # delete any old xml file in that folder
                for file_name in os.listdir(xml_folder):
                    # construct full file path
                    file = xml_folder + file_name
                    if os.path.isfile(file):
                        os.remove(file)

                with NamedTemporaryFile(
                    mode="w+t", delete=False, dir=xml_folder
                ) as tmpfile:
                    for i in xp.run():
                        tmpfile.write(i)
                    filename = tmpfile.name

                res = http.Stream(
                    type="path",
                    path=filename,
                    download_name="odoo_data_for_frepple",
                ).get_response(
                    mimetype="application/xml;charset=utf8",
                    as_attachment=False,
                )
                res.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                res.headers["Pragma"] = "no-cache"
                res.headers["Expires"] = "0"
                return res
            except Exception as e:
                logger.exception("Error generating frePPLe XML data")
                raise InternalServerError(
                    description="Error generating frePPLe XML data:<br>%s"
                    % (
                        traceback.format_exc()
                        if company and company.disclose_stack_trace
                        else e
                    )
                )
        elif req.httprequest.method == "POST":
            # Import the data
            try:
                # Option 1: read the data using the inbound.py file in the same folder
                # from odoo.addons.frepple.controllers.inbound import importer

                # Option 2: Read the data using inbound.py in github.
                # This can be handy during development phase to skip redeploying the connectors after each commit.
                # To enable:
                #   - Update variable raw_url below with the odoo connectors github repo you want to use.
                #   - You should ONLY use sources you trust 100%.
                #     Using untrusted sources allows attackers to execute arbitrary code on your server, and
                #     is a very big security risk.
                #
                class_dict = load_classes_from_github(
                    "https://raw.githubusercontent.com/frePPLe/odoo_sana_fork/refs/heads/17.0/frepple/controllers/inbound.py",
                    ["importer"],
                )
                importer = class_dict["importer"]

                ip = importer(
                    req,
                    database=database,
                    company=company,
                    mode=req.httprequest.form.get("mode", 1),
                    disclose_stack_trace=company and company.disclose_stack_trace,
                )

                return req.make_response(
                    ip.run(),
                    [
                        ("Content-Type", "text/plain"),
                        ("Cache-Control", "no-cache, no-store, must-revalidate"),
                        ("Pragma", "no-cache"),
                        ("Expires", "0"),
                    ],
                )
            except Exception as e:
                logger.exception("Error processing data posted by frePPLe")
                raise InternalServerError(
                    description="Error processing data posted by frePPLe:<br>%s"
                    % (
                        traceback.format_exc()
                        if company and company.disclose_stack_trace
                        else e
                    )
                )
