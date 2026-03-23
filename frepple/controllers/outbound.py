# -*- coding: utf-8 -*-
#
# Copyright (C) 2014 by frePPLe bv
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

import json
import logging
import pytz
import xmlrpc.client
from xml.sax.saxutils import quoteattr
from datetime import datetime, timedelta
from pytz import timezone
import ssl

try:
    import odoo
except ImportError:
    pass

logger = logging.getLogger(__name__)


class Odoo_generator:
    def __init__(self, env):
        self.env = env

    def setContext(self, **kwargs):
        t = dict(self.env.context)
        t.update(kwargs)
        self.env = self.env(
            user=self.env.user,
            context=t,
        )

    def callMethod(self, model, id, method, args=[]):
        for obj in self.env[model].browse(id):
            return getattr(obj, method)(*args)
        return None

    def getData(
        self,
        model,
        search=None,
        order=None,
        fields=None,
        ids=None,
        object=False,
        limit=None,
        offset=0,
    ):
        if search is None:
            search = []
        if fields is None:
            fields = []
        else:
            invalid_fields = [f for f in fields if f not in self.env[model]._fields]
            if invalid_fields:
                logger.warning(f"Unavailable fields {invalid_fields} in {model} model")
        if ids is not None:
            if object:
                return self.env[model].browse(ids) if ids else []
            else:
                return (
                    self.env[model]
                    .browse(ids)
                    .read([f for f in fields if f in self.env[model]._fields])
                    if ids
                    else []
                )
        if order:
            if object:
                return self.env[model].search(
                    search, order=order, limit=limit, offset=offset
                )
            else:
                return (
                    self.env[model]
                    .search(search, order=order, limit=limit, offset=offset)
                    .read([f for f in fields if f in self.env[model]._fields])
                )
        else:
            if object:
                return self.env[model].search(search, limit=limit, offset=offset)
            else:
                return (
                    self.env[model]
                    .search(search, limit=limit, offset=offset)
                    .read([f for f in fields if f in self.env[model]._fields])
                )


class XMLRPC_generator:
    pagesize = 5000

    def __init__(self, url, db, username, password):
        self.db = db
        self.password = password
        self.env = xmlrpc.client.ServerProxy(
            "{}/xmlrpc/2/common".format(url),
            context=ssl._create_unverified_context(),
        )
        self.uid = self.env.authenticate(db, username, password, {})
        self.env = xmlrpc.client.ServerProxy(
            "{}/xmlrpc/2/object".format(url),
            context=ssl._create_unverified_context(),
            use_builtin_types=True,
            headers={"Connection": "keep-alive"}.items(),
        )
        self.context = {}

    def setContext(self, **kwargs):
        self.context.update(kwargs)

    def callMethod(self, model, id, method, args):
        return self.env.execute_kw(
            self.db, self.uid, self.password, model, method, [id], []
        )

    def getData(self, model, search=None, order="id asc", fields=None, ids=None):
        if search is None:
            search = []
        if fields is None:
            fields = []
        if ids:
            page_ids = [ids]
        else:
            page_ids = []
            offset = 0
            msg = {
                "limit": self.pagesize,
                "offset": offset,
                "context": self.context,
                "order": order,
            }
            while True:
                extra_ids = self.env.execute_kw(
                    self.db,
                    self.uid,
                    self.password,
                    model,
                    "search",
                    [search] if search else [[]],
                    msg,
                )
                if not extra_ids:
                    break
                page_ids.append(extra_ids)
                offset += self.pagesize
                msg["offset"] = offset
        if page_ids and page_ids != [[]]:
            data = []
            for page in page_ids:
                data.extend(
                    self.env.execute_kw(
                        self.db,
                        self.uid,
                        self.password,
                        model,
                        "read",
                        [page],
                        {"fields": fields, "context": self.context},
                    )
                )
            return data
        else:
            return []


class exporter(object):
    def __init__(
        self,
        generator,
        req,
        uid,
        database=None,
        company=None,
        mode=1,
        timezone=None,
        singlecompany=False,
        version="0.0.0.unknown",
        delta=999,
        language="en_US",
        apps="",
    ):
        self.database = database
        self.company = company
        self.generator = generator
        self.version = version
        self.timezone = timezone
        if timezone:
            if timezone not in pytz.all_timezones:
                logger.warning("Invalid timezone URL argument: %s." % (timezone,))
                self.timezone = None
            else:
                # Valid timezone override in the url
                self.timezone = timezone
        if not self.timezone:
            # Default timezone: use the timezone of the connector user (or UTC if not set)
            for i in self.generator.getData(
                "res.users",
                ids=[uid],
                fields=["tz"],
            ):
                self.timezone = i["tz"] or "UTC"
        self.timeformat = "%Y-%m-%dT%H:%M:%S"
        self.singlecompany = singlecompany
        self.delta = delta
        self.language = language
        self.has_expiry = (
            "expiration_date" in [f for f in self.generator.env["stock.lot"]._fields]
            and "freppledb.shelflife" in apps
        )
        self.has_length_limits = self.version[0] < 9 or (
            self.version[0] == 9 and self.version[1] < 11
        )

        # The mode argument defines different types of runs:
        #  - Mode 1:
        #    This mode returns all data that is loaded with every planning run.
        #    Currently this mode transfers all objects, except closed sales orders.
        #  - Mode 2:
        #    This mode returns data that is loaded that changes infrequently and
        #    can be transferred during automated scheduled runs at a quiet moment.
        #    Currently this mode transfers only closed sales orders.
        #
        # Normally an Odoo object should be exported by only a single mode.
        # Exporting a certain object with BOTH modes 1 and 2 will only create extra
        # processing time for the connector without adding any benefits. On the other
        # hand it won't break things either.
        #
        # Which data elements belong to each mode can vary between implementations.
        self.mode = mode

    def run(self):
        # Check if we manage by work orders or manufacturing orders.
        self.manage_work_orders = False

        # Load some auxiliary data in memory
        self.load_company()
        if self.mode == 0:
            # This was only a connection test
            yield '<?xml version="1.0" encoding="UTF-8" ?>\n'
            yield '<plan xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" source="odoo_%s">' % self.mode
            yield "connection ok"
            yield "</plan>"
            return

        # Header.
        # The source attribute is set to 'odoo_<mode>', such that all objects created or
        # updated from the data are also marked as from originating from odoo.
        yield '<?xml version="1.0" encoding="UTF-8" ?>\n'
        yield '<plan xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" source="odoo_%s">\n' % self.mode
        yield "<description>Generated by odoo %s</description>\n" % odoo.release.version

        self.currentdate = datetime.now()
        yield "<current>%s</current>" % self.currentdate.strftime("%Y-%m-%dT%H:%M:%S")

        # Synchronize users.
        # This needs to run before we restrict the context to the selected company!
        yield from self.export_users()

        if self.singlecompany:
            # Create a new context to limit the data to the selected company
            self.generator.setContext(allowed_company_ids=[self.company_id])

        self.load_uom()

        # Main content.
        # The order of the entities is important. First one needs to create the
        # objects before they are referenced by other objects.
        # If multiple types of an entity exists (eg operation_time_per,
        # operation_alternate, operation_alternate, etc) the reference would
        # automatically create an object, potentially of the wrong type.
        logger.debug("Exporting calendars.")
        if self.mode == 1:
            yield from self.export_calendar()
        logger.debug("Exporting locations.")
        yield from self.export_locations()
        self.load_operation_types()
        logger.debug("Exporting customers.")
        yield from self.export_customers()
        if self.mode == 1:
            logger.debug("Exporting suppliers.")
            yield from self.export_suppliers()

        logger.debug("Exporting products.")
        yield from self.export_item_hierarchy()
        yield from self.export_items()

        logger.debug("Exporting sales orders.")
        yield from self.export_salesorders()
        # Uncomment the following lines to create forecast models in frepple
        # logger.debug("Exporting forecast.")
        # for i in self.export_forecasts():
        #     yield i
        if self.mode == 1:
            logger.debug("Exporting purchase orders.")
            yield from self.export_purchaseorders()

            if self.has_expiry:
                logger.debug("Exporting stock orders.")
                yield from self.export_stockorders()
            else:
                logger.debug("Exporting quantities on-hand.")
                yield from self.export_onhand()

        # Footer
        yield "</plan>\n"

    def load_company(self):
        self.company_id = 0
        for i in self.generator.getData(
            "res.company",
            search=[("name", "=", self.company)],
            fields=[
                "security_lead",
                "po_lead",
                # "manufacturing_lead",
                "calendar",
                # "manufacturing_warehouse",
                "respect_reservations",
            ],
        ):
            self.company_id = i["id"]
            self.security_lead = int(
                i["security_lead"]
            )  # TODO NOT USED RIGHT NOW - add parameter in frepple for this
            self.po_lead = i["po_lead"]

            self.respect_reservations = i["respect_reservations"]

        if not self.company_id:
            logger.warning("Can't find company '%s'" % self.company)
            self.company_id = None
            self.security_lead = 0
            self.po_lead = 0
            self.manufacturing_lead = 0

    def load_uom(self):
        """
        Loading units of measures into a dictionary for fast lookups.

        All quantities are sent to frePPLe as numbers, expressed in the default
        unit of measure of the uom dimension.
        """
        self.uom = {}
        for i in self.generator.getData(
            "uom.uom",
            # We also need to load INactive UOMs, because there still might be records
            # using the inactive UOM. Questionable practice, but can happen...
            search=["|", ("active", "=", 1), ("active", "=", 0)],
            fields=["factor", "uom_type", "category_id", "name"],
        ):
            self.uom[i["id"]] = {
                "factor": i["factor"],
                "category": i["category_id"][0],
                "name": i["name"],
            }

    def load_operation_types(self):
        """
        Loading operation types into a dictionary for fast lookups.
        """
        self.operation_types = {}
        for i in self.generator.getData(
            "stock.picking.type",
            # We also need to load INactive types
            search=["|", ("active", "=", 1), ("active", "=", 0)],
            fields=[
                "name",
                "sequence_code",
                "code",
                "default_location_src_id",
                "default_location_dest_id",
                "warehouse_id",
            ],
        ):
            self.operation_types[i["id"]] = {
                "id": i["id"],
                "name": i["name"],
                "code": i["code"],
                "sequence_code": i["sequence_code"],
                "default_location_src_id": (
                    self.map_locations.get(i["default_location_src_id"][0], None)
                    if i["default_location_src_id"]
                    else None
                ),
                "default_location_dest_id": (
                    self.map_locations.get(i["default_location_dest_id"][0], None)
                    if i["default_location_dest_id"]
                    else None
                ),
                "warehouse_id": (
                    self.warehouses.get(i["warehouse_id"][0], None)
                    if i["warehouse_id"]
                    else None
                ),
            }

    def convert_qty_uom(self, qty, uom_id, product_template_id=None):
        """
        Convert a quantity to the reference uom of the product template.
        """
        try:
            uom_id = uom_id[0]
        except Exception:
            pass
        if not uom_id:
            return qty
        if not product_template_id:
            return qty * self.uom[uom_id]["factor"]
        try:
            product_uom = self.product_templates[product_template_id]["uom_id"][0]
        except Exception:
            return qty * self.uom[uom_id]["factor"]
        # check if default product uom is the one we received
        if product_uom == uom_id:
            return qty
        # check if different uoms belong to the same category
        if self.uom[product_uom]["category"] == self.uom[uom_id]["category"]:
            return qty / self.uom[uom_id]["factor"] * self.uom[product_uom]["factor"]
        else:
            # UOM is from a different category as the reference uom of the product.
            logger.warning(
                "Can't convert from %s for product template %s"
                % (self.uom[uom_id]["name"], product_template_id)
            )
            return qty * self.uom[uom_id]["factor"]

    def convert_float_time(self, float_time, units="days"):
        """
        Convert Odoo float time to ISO 8601 duration.
        """
        d = timedelta(**{units: float_time})
        return "P%dDT%dH%dM%dS" % (
            d.days,  # duration: days
            int(d.seconds / 3600),  # duration: hours
            int((d.seconds % 3600) / 60),  # duration: minutes
            int(d.seconds % 60),  # duration: seconds
        )

    def formatDateTime(self, d, tmzone=None):
        if not isinstance(d, datetime):
            d = datetime.fromisoformat(d)
        return d.astimezone(timezone(tmzone or self.timezone)).strftime(self.timeformat)

    def export_users(self):
        users = []
        for grp in self.generator.getData(
            "res.groups",
            search=[("name", "=", "frePPLe user")],
            fields=[
                "users",
            ],
        ):
            for usr in self.generator.getData(
                "res.users",
                ids=grp["users"],
                fields=["name", "login", "lang", "company_ids"],
            ):
                if not self.singlecompany or self.company_id in usr["company_ids"]:
                    users.append((usr["name"], usr["login"], usr["lang"]))
        yield '<stringproperty name="users" value=%s/>\n' % quoteattr(json.dumps(users))

    def export_calendar(self):
        """
        Reads all calendars from resource.calendar model and creates a calendar in frePPLe.
        Attendance times are read from resource.calendar.attendance
        Leave times are read from resource.calendar.leaves

        resource.calendar.name -> calendar.name (default value is 0)
        resource.calendar.attendance.date_from -> calendar bucket start date (or 2020-01-01 if unspecified)
        resource.calendar.attendance.date_to -> calendar bucket end date (or 2030-12-31 if unspecified)
        resource.calendar.attendance.hour_from -> calendar bucket start time
        resource.calendar.attendance.hour_to -> calendar bucket end time
        resource.calendar.attendance.dayofweek -> calendar bucket day

        resource.calendar.leaves.date_from -> calendar bucket start date
        resource.calendar.leaves.date_to -> calendar bucket end date

        For two-week calendars all weeks between the calendar start and
        calendar end dates are added in frepple as calendar buckets.
        The week number is using the iso standard (first week of the
        year is the one containing the first Thursday of the year).

        """
        yield "<!-- calendar -->\n"
        yield "<calendars>\n"

        calendars = {}
        cal_tz = {}
        cal_ids = set()
        try:
            # Read the timezone
            for i in self.generator.getData(
                "resource.calendar",
                fields=[
                    "name",
                    "tz",
                ],
            ):
                cal_ids.add(i["id"])
                cal_tz["%s %s" % (i["name"], i["id"])] = i["tz"]

            # Read the resource calendar association
            calendar_resource = {}

            # Read from the attendance/leaves which resource has specific entries
            self.resources_with_specific_calendars = {}
            for i in self.generator.getData(
                "resource.calendar.attendance",
                search=[("resource_id", "!=", False)],
                fields=[
                    "resource_id",
                ],
            ):
                self.resources_with_specific_calendars[i["resource_id"][0]] = i[
                    "resource_id"
                ][1]
            for i in self.generator.getData(
                "resource.calendar.leaves",
                search=[("resource_id", "!=", False), ("time_type", "=", "leave")],
                fields=[
                    "resource_id",
                ],
            ):
                self.resources_with_specific_calendars[i["resource_id"][0]] = i[
                    "resource_id"
                ][1]

            # Read the attendance for all calendars
            for i in self.generator.getData(
                "resource.calendar.attendance",
                search=[("display_type", "=", False)],
                fields=[
                    "dayofweek",
                    "date_from",
                    "date_to",
                    "hour_from",
                    "hour_to",
                    "calendar_id",
                    "week_type",
                    "resource_id",
                    "day_period",
                ],
            ):
                if i["calendar_id"] and i["calendar_id"][0] in cal_ids:
                    calendar_name = "%s %s" % (i["calendar_id"][1], i["calendar_id"][0])

                    if not i["resource_id"]:
                        if calendar_name not in calendars:
                            calendars[calendar_name] = []
                        i["attendance"] = (
                            True
                            if i["day_period"] in ("morning", "afternoon")
                            else False
                        )
                        calendars[calendar_name].append(i)

                    if calendar_resource.get(i["calendar_id"][0]):
                        for res in calendar_resource.get(i["calendar_id"][0]):
                            if i["resource_id"] and res != i["resource_id"][0]:
                                continue
                            if res in self.resources_with_specific_calendars:
                                if (
                                    "calendar for %s"
                                    % (self.resources_with_specific_calendars[res],)
                                    not in calendars
                                ):
                                    calendars[
                                        "calendar for %s"
                                        % (self.resources_with_specific_calendars[res],)
                                    ] = []
                                    cal_tz[
                                        "calendar for %s"
                                        % (self.resources_with_specific_calendars[res],)
                                    ] = cal_tz[calendar_name]
                                i["attendance"] = (
                                    True
                                    if i["day_period"] in ("morning", "afternoon")
                                    else False
                                )
                                calendars[
                                    "calendar for %s"
                                    % (self.resources_with_specific_calendars[res],)
                                ].append(i)

            # Read the leaves for all calendars
            for i in self.generator.getData(
                "resource.calendar.leaves",
                search=[("time_type", "=", "leave")],
                fields=[
                    "date_from",
                    "date_to",
                    "calendar_id",
                    "resource_id",
                ],
            ):
                if i["calendar_id"] and i["calendar_id"][0] in cal_ids:
                    calendar_name = "%s %s" % (i["calendar_id"][1], i["calendar_id"][0])
                    if not i["resource_id"]:
                        if calendar_name not in calendars:
                            calendars[calendar_name] = []
                        i["attendance"] = False
                        calendars[calendar_name].append(i)

                    if calendar_resource.get(i["calendar_id"][0]):
                        for res in calendar_resource.get(i["calendar_id"][0]):
                            if i["resource_id"] and res != i["resource_id"][0]:
                                continue
                            if res in self.resources_with_specific_calendars:
                                if (
                                    "calendar for %s"
                                    % (self.resources_with_specific_calendars[res],)
                                    not in calendars
                                ):
                                    calendars[
                                        "calendar for %s"
                                        % (self.resources_with_specific_calendars[res],)
                                    ] = []
                                    cal_tz[
                                        "calendar for %s"
                                        % (self.resources_with_specific_calendars[res],)
                                    ] = cal_tz[i["calendar_id"][1]]
                                i["attendance"] = False
                                calendars[
                                    "calendar for %s"
                                    % (self.resources_with_specific_calendars[res],)
                                ].append(i)
                # else:
                #    TODO   Handle company-wide leaves that apply to all calendars

            # Iterate over the results:
            for i in calendars:
                priority_attendance = 1000
                priority_leave = 10
                if cal_tz[i] != self.timezone:
                    logger.warning(
                        "timezone is different on workcenter %s and connector user. Working hours will not be synced correctly to frepple."
                        % i
                    )
                yield '<calendar name=%s default="0"><buckets>\n' % quoteattr(i)
                for j in calendars[i]:
                    if j.get("week_type", False) == False:
                        # ONE-WEEK CALENDAR
                        yield '<bucket start="%s" end="%s" value="%s" days="%s" priority="%s" starttime="%s" endtime="%s"/>\n' % (
                            (
                                j["date_from"].strftime("%Y-%m-%dT00:00:00")
                                if j["date_from"]
                                else "2020-01-01T00:00:00"
                            ),
                            (
                                j["date_to"].strftime("%Y-%m-%dT00:00:00")
                                if j["date_to"]
                                else "2030-12-31T00:00:00"
                            ),
                            "1" if j["attendance"] else "0",
                            (
                                (2 ** ((int(j["dayofweek"]) + 1) % 7))
                                if "dayofweek" in j
                                else (2**7) - 1
                            ),
                            priority_attendance if j["attendance"] else priority_leave,
                            # In odoo, monday = 0. In frePPLe, sunday = 0.
                            (
                                ("PT%dM" % round(j["hour_from"] * 60))
                                if "hour_from" in j
                                else "PT0M"
                            ),
                            (
                                ("PT%dM" % round(j["hour_to"] * 60))
                                if "hour_to" in j
                                else "PT1440M"
                            ),
                        )
                        if j["attendance"]:
                            priority_attendance += 1
                        else:
                            priority_leave += 1
                    else:
                        # TWO-WEEKS CALENDAR
                        start = j["date_from"] or datetime(2020, 1, 1)
                        end = j["date_to"] or datetime(2030, 12, 31)

                        t = start
                        while t < end:
                            if int(t.isocalendar()[1] % 2) == int(j["week_type"]):
                                yield '<bucket start="%s" end="%s" value="%s" days="%s" priority="%s" starttime="%s" endtime="%s"/>\n' % (
                                    self.formatDateTime(t, cal_tz[i]),
                                    self.formatDateTime(
                                        min(t + timedelta(7 - t.weekday()), end),
                                        cal_tz[i],
                                    ),
                                    "1",
                                    (
                                        (2 ** ((int(j["dayofweek"]) + 1) % 7))
                                        if "dayofweek" in j
                                        else (2**7) - 1
                                    ),
                                    priority_attendance,
                                    # In odoo, monday = 0. In frePPLe, sunday = 0.
                                    (
                                        ("PT%dM" % round(j["hour_from"] * 60))
                                        if "hour_from" in j
                                        else "PT0M"
                                    ),
                                    (
                                        ("PT%dM" % round(j["hour_to"] * 60))
                                        if "hour_to" in j
                                        else "PT1440M"
                                    ),
                                )
                                priority_attendance += 1
                            dow = t.weekday()
                            t += timedelta(7 - dow)

                yield "</buckets></calendar>\n"

            yield "</calendars>\n"
        except Exception as e:
            logger.info(e)
            yield "</calendars>\n"

    def export_locations(self):
        """
        Generate a list of warehouse locations to frePPLe, based on the
        stock.warehouse model.

        We assume the location name to be unique. This is NOT guaranteed by Odoo.

        The field subcategory is used to store the id of the warehouse. This makes
        it easier for frePPLe to send back planning results directly with an
        odoo location identifier.

        FrePPLe is not interested in the locations odoo defines with a warehouse.
        This methods also populates a map dictionary between these locations and
        warehouse they belong to.

        Mapping:
        stock.warehouse.name -> location.name
        stock.warehouse.id -> location.subcategory
        """
        self.map_locations = {}
        self.warehouses = {}
        first = True
        for i in self.generator.getData(
            "stock.warehouse",
            fields=["name", "code"],
        ):
            if first:
                yield "<!-- warehouses -->\n"
                yield "<locations>\n"
                first = False
            yield '<location name=%s description=%s subcategory="%s"></location>\n' % (
                quoteattr(i["code"]),
                quoteattr(i["name"]),
                i["id"],
            )
            self.warehouses[i["id"]] = i["code"] or i["name"]
        if not first:
            yield "</locations>\n"

        # Populate a mapping location-to-warehouse name for later lookups
        loc_ids = [
            loc["id"]
            for loc in self.generator.getData(
                "stock.location",
                search=[("usage", "=", "internal")],
                fields=["id"],
            )
        ]

        for loc_object in self.generator.getData(
            "stock.location",
            ids=loc_ids,
            fields=["warehouse_id"],
        ):
            if (
                loc_object.get("warehouse_id", False)
                and loc_object["warehouse_id"][0] in self.warehouses
            ):
                self.map_locations[loc_object["id"]] = self.warehouses[
                    loc_object["warehouse_id"][0]
                ]

    def export_customers(self):
        """
        Generate a list of customers to frePPLe, based on the res.partner model.
        We filter on res.partner where customer = True.

        Mapping:
        res.partner.id res.partner.name -> customer.name
        """
        self.map_customers = {}
        # We also build in the loop the supplier map
        self.map_suppliers = {}
        first = True
        individual_inserted = False
        offset = 0
        pagesize = 25000
        while True:
            recs = self.generator.getData(
                "res.partner",
                fields=["name", "parent_id", "is_company"],
                order="parent_id desc",
                offset=offset,
                limit=pagesize,
            )
            if len(recs) == 0:
                break
            offset += pagesize
            for i in recs:

                # We don't kow that parent (archived ?) so continue
                if i["parent_id"] and i["parent_id"][0] not in self.map_customers:
                    continue

                if first:
                    yield "<!-- customers -->\n"
                    yield "<customers>\n"
                    first = False
                if i["is_company"]:
                    name = str(i["id"])
                    supplier = "%s %s" % (i["name"], i["id"])
                    yield '<customer name="%s" description=%s/>\n' % (
                        name,
                        quoteattr(
                            i["name"][:300]
                            if i["name"] and self.has_length_limits
                            else i["name"] or ""
                        ),
                    )
                elif i["parent_id"] == False or i["id"] == i["parent_id"][0]:
                    name = "Individuals"
                    supplier = "Individuals"
                    if not individual_inserted:
                        yield "<customer name=%s/>\n" % quoteattr(name)
                        individual_inserted = True
                else:
                    if i["parent_id"][0] in self.map_customers:
                        name = str(self.map_customers[i["parent_id"][0]])
                        supplier = "%s %s" % (i["parent_id"][1], i["parent_id"][0])
                    else:
                        continue

                self.map_customers[i["id"]] = name
                self.map_suppliers[i["id"]] = supplier

        if not first:
            yield "</customers>\n"

    def export_suppliers(self):
        """
        Generate a list of suppliers for frePPLe, based on the res.partner model.
        We filter on res.supplier where supplier = True.

        Mapping:
        res.partner.id res.partner.name -> supplier.name
        """
        first = True
        for i in set(self.map_suppliers.values()):
            if first:
                yield "<!-- suppliers -->\n"
                yield "<suppliers>\n"
                first = False
            yield "<supplier name=%s/>\n" % quoteattr(i)
        if not first:
            yield "</suppliers>\n"

    def export_item_hierarchy(self):
        """
        Creates an item in frepple for each category that will be then used
        as item.owner

        Mapping:
        product.category.complete_name -> item.name
        product.category.parent_id.complete_name -> item.owner_id
        """
        self.categories = {}
        for i in self.generator.getData(
            "product.category",
            search=[],
            fields=[
                "complete_name",
                "parent_id",
            ],
        ):
            self.categories[i["id"]] = i
        first = True
        for i in self.categories:
            if first:
                yield "<!-- categories -->\n"
                yield "<items>\n"
                first = False
            yield "<item name=%s>%s</item>\n" % (
                quoteattr(self.categories[i]["complete_name"]),
                (
                    (
                        "<owner name=%s/>"
                        % quoteattr(
                            self.categories[self.categories[i]["parent_id"][0]][
                                "complete_name"
                            ]
                        )
                    )
                    if self.categories[i]["parent_id"]
                    else ""
                ),
            )
        if not first:
            yield "</items>\n"

    def export_items(self):
        """
        Send the list of products to frePPLe, based on the product.product model.
        For purchased items we also create a procurement buffer in each warehouse.

        Mapping:
        [product.product.code] product.product.name -> item.name
        product.product.product_tmpl_id.list_price or standard_price -> item.cost
        product.product.id , product.product.product_tmpl_id.uom_id -> item.subcategory

        If product.product.product_tmpl_id.purchase_ok
        we collect the suppliers as product.product.product_tmpl_id.seller_ids
        [product.product.code] product.product.name -> itemsupplier.item
        res.partner.id res.partner.name -> itemsupplier.supplier.name
        supplierinfo.delay -> itemsupplier.leadtime
        supplierinfo.min_qty -> itemsupplier.size_minimum
        supplierinfo.date_start -> itemsupplier.effective_start
        supplierinfo.date_end -> itemsupplier.effective_end
        product.product.product_tmpl_id.delay -> itemsupplier.leadtime
        supplierinfo.sequence -> itemsupplier.priority
        """

        # Read the product templates
        self.product_product = {}
        self.product_template_product = {}
        self.product_templates = {}
        self.routes = {
            i["id"]: i for i in self.generator.getData("stock.route", fields=["name"])
        }
        self.route_mto = None
        for k, v in self.routes.items():
            if v["name"] == "Replenish on Order (MTO)":
                self.route_mto = k
        for i in self.generator.getData(
            "product.template",
            search=[("type", "not in", ("service", "consu", "combo"))],
            fields=[
                "sale_ok",
                "purchase_ok",
                "list_price",
                "standard_price",
                "uom_id",
                "categ_id",
                "product_variant_ids",
                "route_ids",
                "type",
            ]
            + (
                [
                    "expiration_time",
                ]
                if self.has_expiry
                else []
            ),
        ):
            self.product_templates[i["id"]] = i

        # Check if we can use short names
        # To use short names, the internal reference (or the name when no internal reference is defined)
        # needs to be unique
        use_short_names = True

        self.generator.env.cr.execute(
            """
            select count(*) from
            (
            select coalesce(product_product.default_code,
            product_template.name->>%s,
            product_template.name->>'en_US'), count(*)
            from product_product
            inner join product_template on product_product.product_tmpl_id = product_template.id
            where product_template.type not in ('service', 'consu', 'combo')
            group by coalesce(product_product.default_code,
            product_template.name->>%s,
            product_template.name->>'en_US')
            having count(*) > 1
            ) t
                """,
            (self.language, self.language),
        )
        for i in self.generator.env.cr.fetchall():
            if i[0] > 0:
                use_short_names = False
                break

        supplierinfo_fields = [
            "product_tmpl_id",
            "partner_id",
            "delay",
            "min_qty",
            "date_end",
            "date_start",
            "price",
            "batching_window",
            "sequence",
            "is_subcontractor",
        ]
        itemsuppliers = {}
        for i in self.generator.getData(
            "product.supplierinfo",
            fields=supplierinfo_fields,
            search=[("product_tmpl_id", "!=", False)],
            order="sequence, price, delay",
        ):
            if i["product_tmpl_id"][0] in itemsuppliers:
                itemsuppliers[i["product_tmpl_id"][0]].append(i)
            else:
                itemsuppliers[i["product_tmpl_id"][0]] = [i]

        # Read the products
        first = True
        for i in self.generator.getData(
            "product.product",
            fields=[
                "id",
                "name",
                "code",
                "product_tmpl_id",
                "volume",
                "weight",
                "product_template_attribute_value_ids",
                "price_extra",
                "product_replaced_by_id",
            ],
            search=["|", ("active", "=", True), ("active", "=", False)],
        ):
            if first:
                yield "<!-- products -->\n"
                yield "<items>\n"
                first = False
            if i["product_tmpl_id"][0] not in self.product_templates:
                continue
            tmpl = self.product_templates[i["product_tmpl_id"][0]]
            # generate variant name and description in frepple
            if i["product_template_attribute_value_ids"]:
                if use_short_names:
                    name = i["code"] or i["name"]
                    description = i["name"]
                else:
                    name = (
                        (("[%s] %s %s" % (i["code"], i["name"], i["id"])))
                        if i["code"]
                        else "%s %s" % (i["name"], i["id"])
                    )
                    description = None
            # generate name and description for non-variant products
            elif i["code"]:
                name = (
                    (("[%s] %s" % (i["code"], i["name"])))
                    if not use_short_names
                    else i["code"]
                )
                description = i["name"] if use_short_names else None
            else:
                name = i["name"]
                description = i["name"] if use_short_names else None
            if self.has_length_limits:
                name = name[:300]
                if description:
                    description = description[:500]
            prod_obj = {
                "name": name,
                "template": i["product_tmpl_id"][0],
                "product_template_attribute_value_ids": i[
                    "product_template_attribute_value_ids"
                ],
                "code": i["code"],
            }
            self.product_product[i["id"]] = prod_obj
            self.product_template_product[i["product_tmpl_id"][0]] = prod_obj

            # For make-to-order items the next line needs to XML snippet ' type="item_mto"'.
            yield '<item name=%s %s uom=%s volume="%f" weight="%f" cost="%f" subcategory="%s,%s"%s%s>%s\n' % (
                quoteattr(name),
                (
                    ("description=%s" % (quoteattr(description),))
                    if use_short_names
                    else ""
                ),
                quoteattr(tmpl["uom_id"][1]) if tmpl["uom_id"] else "",
                i["volume"] or 0,
                i["weight"] or 0,
                max(
                    0, (tmpl["list_price"] + (i["price_extra"] or 0)) or 0
                )  # Option 1:  Map "sales price" to frepple
                #  max(0, tmpl["standard_price"]) or 0)  # Option 2: Map the "cost" to frepple
                / self.convert_qty_uom(1.0, tmpl["uom_id"], i["product_tmpl_id"][0]),
                tmpl["uom_id"][0],
                i["id"],
                ' type="item_mto"' if self.route_mto in tmpl["route_ids"] else "",
                (
                    (
                        ' shelflife="%s"'
                        % self.convert_float_time(tmpl["expiration_time"])
                    )
                    if self.has_expiry
                    and tmpl["expiration_time"]
                    and tmpl["expiration_time"] > 0
                    else ""
                ),
                (
                    (
                        "<owner name=%s/>"
                        % quoteattr(
                            self.categories[tmpl["categ_id"][0]]["complete_name"]
                        )
                    )
                    if tmpl["categ_id"] and tmpl["categ_id"][0] in self.categories
                    else ""
                ),
            )

            if i.get("product_replaced_by_id"):
                yield f'<stringproperty name="product_replaced_by_id" value="{i.get("product_replaced_by_id")[0]}"/>\n'

            # Export suppliers for the item, if the item is allowed to be purchased
            if tmpl["purchase_ok"]:
                suppliers = {}
                sequence = 0
                for sup in itemsuppliers.get(tmpl["id"], []):
                    sequence += 1
                    name = self.map_suppliers.get(sup["partner_id"][0], None)
                    if not name:
                        # Skip uninterested suppliers (eg archived ones)
                        continue
                    if sup.get("is_subcontractor", False):
                        if not hasattr(tmpl, "subcontractors"):
                            tmpl["subcontractors"] = []
                        tmpl["subcontractors"].append(
                            {
                                "name": name,
                                "delay": sup["delay"],
                                "priority": sequence,
                                "size_minimum": sup["min_qty"],
                            }
                        )
                    elif (name, sup["date_start"]) in suppliers:
                        # If there are multiple records with the same supplier & start date
                        # we pass a single record to frepple with lowest-lead-time,
                        # lowest-quantity, lowest-sequence, greatest-end-date.
                        r = suppliers[(name, sup["date_start"])]
                        if sup["delay"] and (
                            not r["delay"] or sup["delay"] < r["delay"]
                        ):
                            r["delay"] = sup["delay"]
                        if sup["batching_window"] and (
                            not r["batching_window"]
                            or sup["batching_window"] > r["batching_window"]
                        ):
                            r["batching_window"] = sup["batching_window"]
                        if sup["min_qty"] and (
                            not r["min_qty"] or sup["min_qty"] < r["min_qty"]
                        ):
                            r["min_qty"] = sup["min_qty"]
                        if sup["price"] and (
                            not r["price"] or sup["price"] < r["price"]
                        ):
                            r["price"] = sup["price"]
                        if sup["date_end"] and (
                            not r["date_end"] or sup["date_end"] > r["date_end"]
                        ):
                            r["date_end"] = sup["date_end"]
                    else:
                        suppliers[(name, sup["date_start"])] = {
                            "delay": sup["delay"],
                            "sequence": sequence,
                            "batching_window": sup["batching_window"] or 0,
                            "min_qty": sup["min_qty"],
                            "price": max(0, sup["price"]),
                            "date_end": sup["date_end"],
                        }
                if suppliers:
                    yield "<itemsuppliers>\n"
                    for k, v in suppliers.items():
                        if v["date_end"] and v["date_end"] < self.currentdate:
                            continue
                        yield '<itemsupplier leadtime="P%dD" priority="%s" batchwindow="P%dD" size_minimum="%f" size_multiple="%f" cost="%f"%s%s><supplier name=%s/></itemsupplier>\n' % (
                            v["delay"],
                            v["sequence"] or 1,
                            v["batching_window"] or 0,
                            v["min_qty"],
                            v["min_qty"],
                            max(0, v["price"]),
                            (
                                ' effective_end="%sT00:00:00"'
                                % v["date_end"].strftime("%Y-%m-%d")
                                if v["date_end"]
                                else ""
                            ),
                            (
                                ' effective_start="%sT00:00:00"'
                                % k[1].strftime("%Y-%m-%d")
                                if k[1]
                                else ""
                            ),
                            quoteattr(k[0]),
                        )
                    yield "</itemsuppliers>\n"
            yield "</item>\n"
        if not first:
            yield "</items>\n"

    def export_salesorders(self):
        """
        Send confirmed sales order lines as demand to frePPLe, using the
        sale.order and sale.order.line models.

        Each order is linked to a warehouse, which is used as the location in
        frePPLe.

        Only orders in the status 'draft' and 'sale' are extracted.

        The picking policy 'complete' is supported at the sales order line
        level only in frePPLe. FrePPLe doesn't allow yet to coordinate the
        delivery of multiple lines in a sales order (except with hacky
        modeling construct).
        The field requested_date is only available when sale_order_dates is
        installed.

        Mapping:
        sale.order.name ' ' sale.order.line.id -> demand.name
        sales.order.requested_date -> demand.due
        '1' -> demand.priority
        [product.product.code] product.product.name -> demand.item
        sale.order.partner_id.name -> demand.customer
        convert sale.order.line.product_uom_qty and sale.order.line.product_uom  -> demand.quantity
        stock.warehouse.name -> demand->location
        (if sale.order.picking_policy = 'one' then same as demand.quantity else 1) -> demand.minshipment
        """

        # sales orders to exclude:
        so_to_exclude = [
            "S03050",
            "S03294",
            "S03162",
            "S03100",
            "S03339",
            "S03598",
            "S03172",
            "S03319",
            "S03784",
            "S03692",
            "S03201",
            "S03500",
            "S03224",
            "S03427",
            "S03670",
            "S03204",
            "S03186",
            "S03691",
            "S03349",
            "S03134",
            "S03089",
            "S03463",
            "S03219",
            "S03415",
            "S03223",
            "S03270",
            "S03506",
            "S03827",
            "S03822",
            "S05203",
        ]

        # Get all sales order lines
        search = (
            [
                ("product_id", "!=", False),
                ("order_id.state", "not in", ["draft", "sent", "cancel"]),
                ("order_id.partner_id.id", "!=", 4887),
                ("order_id.name", "not in", so_to_exclude),
            ]
            if self.delta >= 999
            else [
                ("product_id", "!=", False),
                ("order_id.state", "not in", ["draft", "sent", "cancel"]),
                (
                    "write_date",
                    ">=",
                    datetime.now() - timedelta(days=self.delta),
                ),
                ("order_id.partner_id.id", "!=", 4887),
                ("order_id.name", "not in", so_to_exclude),
            ]
        )
        so_line = self.generator.getData(
            "sale.order.line",
            search=search,
            fields=[
                "qty_delivered",
                "state",
                "product_id",
                "product_uom_qty",
                "product_uom",
                "order_id",
                "move_ids",
            ],
        )

        # Get all sales orders
        so = {
            i["id"]: i
            for i in self.generator.getData(
                "sale.order",
                ids=[j["order_id"][0] for j in so_line],
                fields=[
                    "state",
                    "partner_id",
                    "commitment_date",
                    "date_order",
                    "picking_policy",
                    "warehouse_id",
                ],
            )
        }

        # Get all move ids
        # We only read the open ones

        stock_moves_dict = {
            i["id"]: i
            for i in self.generator.getData(
                "stock.move",
                search=[
                    (
                        "state",
                        "in",
                        ["waiting", "partially_available", "assigned", "confirmed"],
                    )
                ],
                fields=[
                    "id",
                    "move_orig_ids",
                    "product_id",
                    "date",
                    "quantity",
                    "procure_method",
                    "product_uom_qty",
                    "product_uom",
                    "state",
                ],
            )
        }

        def getReservedQuantity(stock_move_id):
            reserved_quantity = 0
            if stock_move_id in stock_moves_dict:
                mv = stock_moves_dict[stock_move_id]
                reserved_quantity = mv["quantity"] or 0
                for i in mv["move_orig_ids"]:
                    if i != stock_move_id:
                        reserved_quantity += getReservedQuantity(i)
            return reserved_quantity

        # Generate the demand records
        yield "<!-- sales order lines -->\n"
        yield "<demands>\n"

        for i in so_line:
            name = "%s %d" % (i["order_id"][1], i["id"])
            batch = i["order_id"][1]
            product = (
                self.product_product.get(i["product_id"][0], None)
                if i["product_id"]
                else None
            )
            j = so[i["order_id"][0]]
            location = "SANA"
            customer = (
                self.map_customers.get(j["partner_id"][0], None)
                if j["partner_id"]
                else None
            )

            if not customer or not location or not product:
                # Not interested in this sales order...
                continue
            due = self.formatDateTime(
                j.get("commitment_date", False) or j["date_order"]
            )
            priority = 1  # We give all customer orders the same default priority

            # Possible sales order status are 'draft', 'sent', 'sale', 'done' and 'cancel'

            # if no stock_move if that SO line is still open, we can consider the line closed
            state = j.get("state", "sale")
            if state == "sale" and not any(
                x in stock_moves_dict and stock_moves_dict[x] not in ("cancel", "done")
                for x in i["move_ids"]
            ):
                state = "done"
            if state in ("draft", "sent"):
                # status = "inquiry"  # Inquiries don't reserve capacity and materials
                status = "quote"  # Quotes do reserve capacity and materials
                qty = self.convert_qty_uom(
                    i["product_uom_qty"],
                    i["product_uom"],
                    product["template"],
                )
            elif state == "sale":
                if i["move_ids"] and any(
                    [mv_id in stock_moves_dict for mv_id in i["move_ids"]]
                ):
                    for mv_id in i["move_ids"]:
                        sol_name = (
                            "%s %s" % (name, mv_id) if len(i["move_ids"]) > 1 else name
                        )
                        sm = stock_moves_dict.get(mv_id)
                        if sm:
                            sm_product = (
                                self.product_product.get(sm["product_id"][0], None)
                                if sm["product_id"]
                                else product
                            )
                            if not sm_product:
                                continue
                            qty = self.convert_qty_uom(
                                sm["product_uom_qty"],
                                sm["product_uom"],
                                sm_product["template"],
                            )
                            reserved_quantity = (
                                getReservedQuantity(mv_id)
                                if self.respect_reservations
                                else 0
                            )
                            due = self.formatDateTime(sm["date"] or j["date_order"])

                            yield (
                                '<demand name=%s batch=%s quantity="%s" due="%s" priority="%s" minshipment="%s" status="%s"><item name=%s/><customer name=%s/><location name=%s/>'
                                # Disable the next line in frepple < 6.25
                                '<owner name=%s policy="%s" xsi:type="demand_group"/>'
                                "</demand>\n"
                            ) % (
                                quoteattr(sol_name),
                                quoteattr(batch),
                                (
                                    qty - reserved_quantity
                                    if qty - reserved_quantity > 0
                                    else qty
                                ),
                                due,
                                priority,
                                (
                                    qty - reserved_quantity
                                    if j["picking_policy"] == "one"
                                    and qty - reserved_quantity > 0
                                    else 0.0
                                ),
                                "open" if qty - reserved_quantity > 0 else "closed",
                                quoteattr(sm_product["name"]),
                                quoteattr(customer),
                                quoteattr(location),
                                # Disable the next 2 lines in frepple < 6.25
                                quoteattr(i["order_id"][1]),
                                (
                                    "alltogether"
                                    if j["picking_policy"] == "one"
                                    else "independent"
                                ),
                            )
                    # We are done with this line, move to the next one
                    continue
                else:
                    qty = i["product_uom_qty"] - i["qty_delivered"]
                    if qty <= 0:
                        status = "closed"
                        qty = self.convert_qty_uom(
                            i["product_uom_qty"],
                            i["product_uom"],
                            product["template"],
                        )
                    else:
                        status = "open"
                        qty = self.convert_qty_uom(
                            qty,
                            i["product_uom"],
                            product["template"],
                        )
            elif state == "done":
                status = "closed"
                qty = self.convert_qty_uom(
                    i["product_uom_qty"],
                    i["product_uom"],
                    product["template"],
                )
            elif state == "cancel":
                status = "canceled"
                qty = self.convert_qty_uom(
                    i["product_uom_qty"],
                    i["product_uom"],
                    product["template"],
                )
            else:
                logger.warning("Unknown sales order state: %s." % (state,))
                continue

            yield (
                '<demand name=%s batch=%s quantity="%s" due="%s" priority="%s" minshipment="%s" status="%s"><item name=%s/><customer name=%s/><location name=%s/>'
                # Disable the next line in frepple < 6.25
                '<owner name=%s policy="%s" xsi:type="demand_group"/>'
                "</demand>\n"
            ) % (
                quoteattr(name),
                quoteattr(batch),
                qty,
                due,
                priority,
                qty if j["picking_policy"] == "one" and qty > 0 else 0.0,
                status,
                quoteattr(product["name"]),
                quoteattr(customer),
                quoteattr(location),
                # Disable the next lines in frepple < 6.25
                quoteattr(i["order_id"][1]),
                "alltogether" if j["picking_policy"] == "one" else "independent",
            )
        yield "</demands>\n"

    def export_forecasts(self):
        """
        IMPORTANT:
        Only use this in the frepple Enterprise and Cloud Editions.
        And only use it when the parameter "forecast.populateForecastTable" is set to false.

        Sends the list of forecasts to frepple based on odoo's sellable products.

        This method will need customization for each deployment.
        """
        yield "<!-- forecasts -->\n"
        yield "<demands>\n"
        for prod in self.product_product.values():
            if (
                not prod["template"]
                or not self.product_templates[prod["template"]]["sale_ok"]
            ):
                continue
            yield (
                '<demand name=%s planned="true" xsi:type="demand_forecast">'
                "<item name=%s/><location name=%s /><customer name=%s />"
                "<methods>%s</methods>"
                "</demand>"
            ) % (
                quoteattr(prod["name"]),
                quoteattr(prod["name"]),
                quoteattr("Chicago 1"),  # Edit to location name where to forecast
                quoteattr("All customers"),  # Edit to customer name to forecast for
                "manual",  # Values:   "manual" for user entered forecasts, "automatic" for calculating statistical forecasts
            )
        yield "</demands>\n"

    def export_purchaseorders(self):
        """
        Send all open purchase orders to frePPLe, using the purchase.order and
        purchase.order.line models.

        Only purchase order lines in state 'confirmed' are extracted. The state of the
        purchase order header must be "approved".

        Mapping:
        purchase.order.line.product_id -> operationplan.item
        purchase.order.company.mfg_location -> operationplan.location
        purchase.order.partner_id -> operationplan.supplier
        convert purchase.order.line.product_uom_qty - purchase.order.line.qty_received and purchase.order.line.product_uom -> operationplan.quantity
        purchase.order.date_planned -> operationplan.end
        purchase.order.date_planned -> operationplan.start
        'PO' -> operationplan.ordertype
        'confirmed' -> operationplan.status
        """
        self.subcontracting_mo_po_mapping = {}
        po_line = {
            i["id"]: i
            for i in self.generator.getData(
                "purchase.order.line",
                search=[
                    "|",
                    (
                        "order_id.state",
                        "not in",
                        # Comment out on of the following alternative approaches:
                        # Alternative I: don't send RFQs to frepple because that supply isn't certain to be available yet.
                        ("draft", "sent", "bid", "to approve", "confirmed", "cancel"),
                        # Alternative II: send RFQs to frepple to avoid that the same purchasing proposal is generated again by frepple.
                        # ("bid", "confirmed", "cancel"),
                    ),
                    ("order_id.state", "=", False),
                    "|",
                    ("order_id.receipt_status", "!=", "full"),
                    ("order_id.receipt_status", "=", False),
                ],
                object=True,
            )
        }

        yield "<!-- open purchase orders -->\n"
        yield "<operationplans>\n"
        for i in po_line.values():
            if i.move_ids:
                # METHOD 1: Use the stock move information rather than the po line
                for mv in i.move_ids:
                    if (
                        not mv.product_id
                        or not mv.purchase_line_id
                        or not mv.location_dest_id
                        or mv.state in ("draft", "cancel", "done")
                    ):
                        continue
                    j = mv.purchase_line_id.order_id
                    po_line_reference = "%s - %s - %s - %s" % (
                        j.name,
                        mv.picking_id.name,
                        mv.id,
                        mv.purchase_line_id.id,
                    )
                    if getattr(mv, "is_subcontract", False):
                        # PO lines on a subcontracting BOM are mapped as a MO in frepple
                        for k in mv.move_orig_ids:
                            if k.production_id:
                                self.subcontracting_mo_po_mapping[
                                    k.production_id.id
                                ] = po_line_reference
                        continue
                    item = self.product_product.get(mv.product_id.id, None)
                    if not item:
                        continue

                    # MTO links
                    if (
                        self.route_mto
                        in self.product_templates[item["template"]]["route_ids"]
                    ):
                        mto_so = mv.move_dest_ids.group_id.sale_id
                        batch = mto_so[0].name if mto_so else None
                        if not batch:
                            mto_mo = j._get_mrp_productions()
                            if mto_mo:
                                batch = mto_mo[0].display_name
                    else:
                        batch = None

                    location = (
                        "SANA"  # self.map_locations.get(mv.location_dest_id.id, None)
                    )

                    if not location:
                        continue
                    start = j.date_order
                    if not isinstance(start, datetime):
                        start = datetime.fromisoformat(start)
                    end = mv.date
                    if not isinstance(end, datetime):
                        end = datetime.fromisoformat(end)
                    start = self.formatDateTime(start if start < end else end)
                    end = self.formatDateTime(end)
                    qty = mv.product_qty
                    supplier = self.map_suppliers.get(j.partner_id.id)
                    if not supplier:
                        # supplier is archived :-(
                        for sup in self.generator.getData(
                            "res.partner",
                            search=[
                                ("id", "=", j.partner_id.id),
                                "|",
                                ("active", "=", True),
                                ("active", "=", False),
                            ],
                            fields=["name", "active"],
                        ):
                            supplier = "%s %s%s" % (
                                sup["name"],
                                "(archived) " if not sup["active"] else "",
                                sup["id"],
                            )
                            self.map_suppliers[sup["id"]] = supplier
                            break
                    if not supplier:
                        continue
                    if qty >= 0:
                        yield '<operationplan reference=%s %sordertype="PO" start="%s" end="%s" quantity="%f" status="confirmed">' "<item name=%s/><location name=%s/><supplier name=%s/></operationplan>\n" % (
                            quoteattr(po_line_reference),
                            "batch=%s " % quoteattr(batch) if batch else "",
                            start,
                            end,
                            qty,
                            quoteattr(item["name"]),
                            quoteattr(location),
                            quoteattr(supplier),
                        )
            else:
                # METHOD 2: Create purchasing operations from purchase order lines
                if not i["product_id"] or i["state"] == "cancel":
                    continue
                item = self.product_product.get(i.product_id.id, None)
                j = i.order_id
                if not item:
                    continue
                location = "SANA"
                if location and item and i.product_qty > i.qty_received:
                    start = j.date_order
                    if not isinstance(start, datetime):
                        start = datetime.fromisoformat(start)
                    end = i.date_planned
                    if not isinstance(end, datetime):
                        end = datetime.fromisoformat(end)
                    start = self.formatDateTime(start if start < end else end)
                    end = self.formatDateTime(end)
                    qty = self.convert_qty_uom(
                        i.product_qty - i.qty_received,
                        i.product_uom.id,
                        self.product_product[i.product_id.id]["template"],
                    )
                    supplier = self.map_suppliers.get(j.partner_id.id)
                    if not supplier:
                        # supplier is archived :-(
                        for sup in self.generator.getData(
                            "res.partner",
                            search=[
                                ("id", "=", j.partner_id.id),
                                "|",
                                ("active", "=", True),
                                ("active", "=", False),
                            ],
                            fields=["name", "active"],
                        ):
                            supplier = "%s %s%s" % (
                                sup["name"],
                                "(archived) " if not sup["active"] else "",
                                sup["id"],
                            )
                            self.map_suppliers[sup["id"]] = supplier
                            break
                    if not supplier:
                        continue

                    # MTO links
                    if (
                        self.route_mto
                        in self.product_templates[item["template"]]["route_ids"]
                    ):
                        mto_so = i.move_dest_ids.group_id.sale_id
                        batch = mto_so[0].name if mto_so else None
                        if not batch:
                            mto_mo = j._get_mrp_productions()
                            if mto_mo:
                                batch = mto_mo[0].display_name
                    else:
                        batch = None

                    yield '<operationplan reference=%s %sordertype="PO" start="%s" end="%s" quantity="%f" status="confirmed">' "<item name=%s/><location name=%s/><supplier name=%s/></operationplan>\n" % (
                        quoteattr("%s - %s" % (j.name, i.id)),
                        "batch=%s " % quoteattr(batch) if batch else "",
                        start,
                        end,
                        qty,
                        quoteattr(item["name"]),
                        quoteattr(location),
                        quoteattr(supplier),
                    )
        yield "</operationplans>\n"

    def export_orderpoints(self):
        """
        Defining order points for frePPLe, based on the stock.warehouse.orderpoint
        model.

        Mapping:
        stock.warehouse.orderpoint.product.name ' @ ' stock.warehouse.orderpoint.location_id.name -> buffer.name
        stock.warehouse.orderpoint.location_id.name -> buffer.location
        stock.warehouse.orderpoint.product.name -> buffer.item
        convert stock.warehouse.orderpoint.product_min_qty -> buffer.mininventory
        convert stock.warehouse.orderpoint.product_max_qty -> buffer.maxinventory
        convert stock.warehouse.orderpoint.qty_multiple -> buffer->size_multiple
        """
        first = True
        # Keeping with the original reorderpoint mapping now
        # try:
        #     has_buffer_max = self.version[0] >= 9
        # except Exception:
        #     has_buffer_max = False
        has_buffer_max = False

        if has_buffer_max:
            # frepple >= 9.0 has native support for buffers with a min and max level
            for i in self.generator.getData(
                "stock.warehouse.orderpoint",
                fields=[
                    "warehouse_id",
                    "product_id",
                    "product_min_qty",
                    "product_max_qty",
                    "product_uom",
                    "qty_multiple",
                ],
            ):
                if first:
                    yield "<!-- order points -->\n"
                    yield "<buffers>\n"
                    first = False
                item = self.product_product.get(
                    i["product_id"] and i["product_id"][0] or 0, None
                )
                if not item:
                    continue
                warehouse = (
                    self.warehouses.get(i["warehouse_id"][0])
                    if i["warehouse_id"]
                    else None
                )
                if not warehouse:
                    continue
                uom_factor = self.convert_qty_uom(
                    1.0,
                    i["product_uom"][0],
                    self.product_product[i["product_id"][0]]["template"],
                )
                yield '<buffer name=%s minimum="%f" maximum="%f"><item name=%s/><location name=%s/></buffer>\n' % (
                    quoteattr("%s @ %s" % (item["name"], warehouse)),
                    ((i["product_min_qty"] or 0) * uom_factor),
                    ((i["product_max_qty"] or 0) * uom_factor),
                    quoteattr(item["name"]),
                    quoteattr(i["warehouse_id"][1]),
                )
            if not first:
                yield "</buffers>\n"
        else:
            for i in self.generator.getData(
                "stock.warehouse.orderpoint",
                fields=[
                    "warehouse_id",
                    "product_id",
                    "product_min_qty",
                    "product_max_qty",
                    "product_uom",
                    "qty_multiple",
                ],
            ):
                if first:
                    yield "<!-- order points -->\n"
                    yield "<calendars>\n"
                    first = False
                item = self.product_product.get(
                    i["product_id"] and i["product_id"][0] or 0, None
                )
                if not item:
                    continue
                warehouse = (
                    self.warehouses.get(i["warehouse_id"][0])
                    if i["warehouse_id"]
                    else None
                )
                if not warehouse:
                    continue
                uom_factor = self.convert_qty_uom(
                    1.0,
                    i["product_uom"][0],
                    self.product_product[i["product_id"][0]]["template"],
                )
                name = "%s @ %s" % (item["name"], warehouse)
                if i["product_min_qty"]:
                    yield """
                    <calendar name=%s default="0"><buckets>
                    <bucket start="%s" end="2030-12-31T00:00:00" value="%s" days="127" priority="998" starttime="PT0M" endtime="PT1440M"/>
                    </buckets>
                    </calendar>\n
                    """ % (
                        (quoteattr("SS for %s" % (name,))),
                        self.currentdate.strftime("%Y-%m-%dT%H:%M:%S"),
                        (i["product_min_qty"] * uom_factor),
                    )
                if i["product_max_qty"] - i["product_min_qty"] > 0:
                    yield """
                    <calendar name=%s default="0"><buckets>
                    <bucket start="%s" end="2030-12-31T00:00:00" value="%s" days="127" priority="998" starttime="PT0M" endtime="PT1440M"/>
                    </buckets>
                    </calendar>\n
                    """ % (
                        (quoteattr("ROQ for %s" % (name,))),
                        self.currentdate.strftime("%Y-%m-%dT%H:%M:%S"),
                        ((i["product_max_qty"] - i["product_min_qty"]) * uom_factor),
                    )
            if not first:
                yield "</calendars>\n"

    # export_stockorders will be called instead of export_onhand
    # when expiration dates is enabled in Odoo

    def export_stockorders(self):
        """
        Extracting all on hand inventories to frePPLe.

        We're bypassing the ORM for performance reasons.

        Mapping:
        stock.report.prodlots.product_id.name @ stock.report.prodlots.location_id.name -> buffer.name
        stock.report.prodlots.product_id.name -> buffer.item
        stock.report.prodlots.location_id.name -> buffer.location
        sum(stock.report.prodlots.qty) -> buffer.onhand
        """
        yield "<!-- inventory -->\n"
        yield "<operationplans>\n"
        if isinstance(self.generator, Odoo_generator):
            # SQL query gives much better performance
            self.generator.env.cr.execute(
                """
                SELECT stock_quant.product_id,
                stock_quant.location_id,
                sum(stock_quant.quantity) as quantity,
                sum(stock_quant.reserved_quantity) as reserved_quantity,
                stock_lot.name as lot_name,
                stock_lot.expiration_date
                FROM stock_quant
                inner join stock_location on stock_location.id = stock_quant.location_id
                and stock_location.scrap_location is distinct from true
                and stock_location.return_location is distinct from true
                left outer join stock_lot on stock_quant.lot_id = stock_lot.id
                and stock_lot.product_id = stock_quant.product_id
                WHERE quantity > 0
                GROUP BY stock_quant.product_id,
                stock_quant.location_id,
                stock_lot.name,
                stock_lot.expiration_date
                ORDER BY location_id ASC
                """
            )
            data = self.generator.env.cr.fetchall()
        else:
            data = [
                (i["product_id"][0], i["location_id"][0], i["quantity"])
                for i in self.generator.getData(
                    "stock.quant",
                    search=[("quantity", ">", 0)],
                    fields=[
                        "product_id",
                        "location_id",
                        "quantity",
                        "reserved_quantity",
                    ],
                )
                if i["product_id"] and i["location_id"]
            ]
        inventory = {}
        expirationdate = {}
        for i in data:
            item = self.product_product.get(i[0], None)
            location = self.map_locations.get(i[1], None)
            lotname = i[4]
            if item and location:
                inventory[(item["name"], location, lotname)] = max(
                    0,
                    inventory.get((item["name"], location, lotname), 0)
                    + i[2]
                    - (i[3] if self.respect_reservations else 0),
                )
                if i[5]:
                    expirationdate[(item["name"], location, lotname)] = i[5]
        for key, val in inventory.items():
            yield (
                """
            <operationplan ordertype="STCK" end="%s" reference=%s %s quantity="%s">
			<item name=%s/>
			<location name=%s/>
		    </operationplan>
            """
                % (
                    self.formatDateTime(datetime.now()),
                    quoteattr(
                        "STCK %s @ %s%s"
                        % (key[0], key[1], (" @ %s" % (key[2],)) if key[2] else "")
                    ),
                    (
                        ('expiry="%s"' % self.formatDateTime(expirationdate[key]))
                        if key in expirationdate
                        else ""
                    ),
                    val or 0,
                    quoteattr(key[0]),
                    quoteattr(key[1]),
                )
            )
        yield "</operationplans>\n"

    # export_stockorders will be called instead of export_onhand
    # when expiration dates is enabled in Odoo

    def export_onhand(self):
        """
        Extracting all on hand inventories to frePPLe.

        We're bypassing the ORM for performance reasons.

        Mapping:
        stock.report.prodlots.product_id.name @ stock.report.prodlots.location_id.name -> buffer.name
        stock.report.prodlots.product_id.name -> buffer.item
        stock.report.prodlots.location_id.name -> buffer.location
        sum(stock.report.prodlots.qty) -> buffer.onhand
        """
        yield "<!-- inventory -->\n"
        yield "<buffers>\n"
        if isinstance(self.generator, Odoo_generator):
            # SQL query gives much better performance
            self.generator.env.cr.execute(
                "SELECT product_id, stock_quant.location_id, sum(quantity), sum(reserved_quantity) "
                "FROM stock_quant "
                "INNER JOIN stock_location ON stock_quant.location_id = stock_location.id "
                "WHERE quantity > 0 "
                "AND stock_location.scrap_location is distinct from true "
                "AND stock_location.return_location is distinct from true "
                "AND stock_location.usage = 'internal' "
                "GROUP BY product_id, stock_quant.location_id "
                "ORDER BY stock_quant.location_id ASC"
            )
            data = self.generator.env.cr.fetchall()
        else:
            data = [
                (i["product_id"][0], i["location_id"][0], i["quantity"])
                for i in self.generator.getData(
                    "stock.quant",
                    search=[("quantity", ">", 0)],
                    fields=[
                        "product_id",
                        "location_id",
                        "quantity",
                        "reserved_quantity",
                    ],
                )
                if i["product_id"] and i["location_id"]
            ]
        inventory = {}
        for i in data:
            item = self.product_product.get(i[0], None)
            location = self.map_locations.get(i[1], None)
            if item and location:
                inventory[(item["name"], location)] = (
                    inventory.get((item["name"], location), 0)
                    + i[2]
                    - (i[3] if self.respect_reservations else 0)
                )
        for key, val in inventory.items():
            buf = "%s @ %s" % (key[0], key[1])
            yield '<buffer name=%s onhand="%f"><item name=%s/><location name=%s/></buffer>\n' % (
                quoteattr(buf),
                val,
                quoteattr(key[0]),
                quoteattr(key[1]),
            )
        yield "</buffers>\n"


if __name__ == "__main__":
    #
    # When calling this script directly as a Python file, the connector uses XMLRPC
    # to connect to odoo and download all data.
    #
    # This is useful for debugging connector updates remotely, when you don't have
    # direct access to the odoo server itself.
    # This mode of working is not recommended for production use because of performance
    # considerations.
    #
    # DEPRECATED EXPERIMENTAL FEATURE!!!
    # This feature was always experimental, and we now see it as a dead end.
    #
    import argparse
    from warnings import warn

    warn("The XMLRPC odoo connector is deprecated", DeprecationWarning)

    parser = argparse.ArgumentParser(description="Debug frepple odoo connector")
    parser.add_argument(
        "--url", help="URL of the odoo server", default="http://localhost:8069"
    )
    parser.add_argument("--db", help="Odoo database to connect to", default="odoo14")
    parser.add_argument(
        "--username", help="User name for the odoo connection", default="admin"
    )
    parser.add_argument(
        "--password", help="User password for the odoo connection", default="admin"
    )
    parser.add_argument(
        "--company", help="Odoo company to use", default="My Company (Chicago)"
    )
    parser.add_argument(
        "--timezone", help="Time zone to convert odoo datetime fields to", default="UTC"
    )
    parser.add_argument(
        "--singlecompany",
        default=False,
        help="Limit the data to a single company only.",
        action="store_true",
    )
    args = parser.parse_args()

    generator = XMLRPC_generator(args.url, args.db, args.username, args.password)
    xp = exporter(
        generator,
        None,
        uid=generator.uid,
        database=generator.db,
        company=args.company,
        mode=1,
        timezone=args.timezone,
        singlecompany=True,
    )
    for i in xp.run():
        print(i, end="")
