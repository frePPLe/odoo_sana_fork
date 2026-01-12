# -*- coding: utf-8 -*-
{
    "name": "freppledata",
    "summary": "Test data for frepple",
    "description": "This addon loads test and demo data for frepple in odoo.",
    "author": "frePPLe",
    "license": "Other OSI approved licence",
    "category": "Uncategorized",
    "version": "16.0.0",
    "depends": ["sale_stock"],
    "data": [
        "data/sale.order.xml",
        "data/purchase.order.xml",
        "data/stock.warehouse.orderpoint.csv",
        "data/product.supplierinfo.xml",
        "data/config.xml",
    ],
    "autoinstall": False,
    "installable": True,
    "price": 0,
    "currency": "EUR",
    "images": ["static/description/images/freppledata.png"],
}
