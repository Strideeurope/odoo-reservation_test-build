from odoo import fields, models


class StockLocation(models.Model):
    _inherit = "stock.location"

    usage = fields.Selection(
        selection_add=[("bin", "Bin (Shadow Ledger)")],
        ondelete={"bin": "cascade"},
    )
