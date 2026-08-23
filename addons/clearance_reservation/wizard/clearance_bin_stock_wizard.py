from odoo import api, fields, models


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
    # Narrows location_id's own view domain when placing a pallet — only
    # Buffer bins with nothing currently tracked in them at all, so a new
    # pallet never gets logged on top of a bin someone else already put
    # something in. Left as every internal location when removing (you're
    # taking FROM wherever something already exists, the opposite
    # condition), and computed fresh each time since it depends on other
    # clearance.bin.stock records, not something cleanly depends()-able.
    available_location_ids = fields.Many2many(
        "stock.location", compute="_compute_available_location_ids"
    )

    @api.depends("mode")
    def _compute_available_location_ids(self):
        buffer_zone = self.env["stock.location"].search([("name", "=", "Buffer Zone")], limit=1)
        all_internal = self.env["stock.location"].search([("usage", "=", "internal")])
        for wizard in self:
            if wizard.mode != "add" or not buffer_zone:
                wizard.available_location_ids = all_internal
                continue
            buffer_bins = self.env["stock.location"].search([
                ("id", "child_of", buffer_zone.id),
                ("id", "!=", buffer_zone.id),
                ("usage", "=", "internal"),
            ])
            occupied = self.env["clearance.bin.stock"].search([
                ("location_id", "in", buffer_bins.ids), ("quantity", ">", 0),
            ]).location_id
            wizard.available_location_ids = buffer_bins - occupied

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
