{
    "name": "Clearance Timestamp Reservation",
    "version": "18.0.1.0.0",
    "depends": ["sale_management", "stock", "account", "purchase"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "views/sale_order_views.xml",
        "views/stock_picking_views.xml",
        "views/product_views.xml",
        "views/clearance_bin_stock_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "clearance_reservation/static/src/stock_forecasted/forecasted_details_clearance.js",
            "clearance_reservation/static/src/stock_forecasted/forecasted_details_clearance.xml",
            "clearance_reservation/static/src/stock_forecasted/forecasted_header_clearance.xml",
            "clearance_reservation/static/src/stock_forecasted/forecasted_buttons_clearance.js",
            "clearance_reservation/static/src/stock_forecasted/forecasted_buttons_clearance.xml",
        ],
    },
    "post_init_hook": "post_init_hook",
    "installable": True,
}
