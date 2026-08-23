from odoo import fields, models


class ClearanceBinStockWizard(models.TransientModel):
    _name = "clearance.bin.stock.wizard"
    _description = "Log a pallet physically placed at, or removed from, a bin"

    mode = fields.Selection(
        [("add", "Place pallet"), ("remove", "Remove pallet")],
        required=True, default="add",
    )
    product_id = fields.Many2one("product.product", required=True)
    location_id = fields.Many2one(
        "stock.location", required=True, domain=[("usage", "=", "internal")],
    )
    quantity = fields.Float(required=True)
    note = fields.Char()

    def action_confirm(self):
        self.ensure_one()
        record = self.env["clearance.bin.stock"]._get_or_create(
            self.product_id, self.location_id
        )
        if self.mode == "add":
            record.add_quantity(self.quantity, note=self.note)
        else:
            record.remove_quantity(self.quantity, note=self.note)
        return {"type": "ir.actions.act_window_close"}
