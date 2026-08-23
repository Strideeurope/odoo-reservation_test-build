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
    # Narrows location_id's own view domain when placing a pallet — Buffer
    # bins (any of them) plus Main's own picking location(s), restricted
    # to ones that are either empty or already tracking THIS SAME
    # product. The point is never mixing two different products in one
    # bin — topping up a bin that already holds more of the same product
    # (including receiving straight into Main) is always fine. Left as
    # every internal location when removing (you're taking FROM wherever
    # something already exists, the opposite condition), and computed
    # fresh each time since it depends on other clearance.bin.stock
    # records, not something cleanly depends()-able.
    available_location_ids = fields.Many2many(
        "stock.location", compute="_compute_available_location_ids"
    )

    @api.depends("mode", "product_id")
    def _compute_available_location_ids(self):
        all_internal = self.env["stock.location"].search([("usage", "=", "internal")])
        candidate_zones = self._get_candidate_zones()
        candidate_bins = self.env["stock.location"]
        for zone in candidate_zones:
            candidate_bins |= self.env["stock.location"].search([
                ("id", "child_of", zone.id), ("usage", "=", "internal"),
            ])
        for wizard in self:
            if wizard.mode != "add" or not candidate_bins:
                wizard.available_location_ids = all_internal
                continue
            blocked = self.env["clearance.bin.stock"].search([
                ("location_id", "in", candidate_bins.ids), ("quantity", ">", 0),
            ])
            if wizard.product_id:
                blocked = blocked.filtered(lambda r: r.product_id != wizard.product_id)
            wizard.available_location_ids = candidate_bins - blocked.location_id

    def _get_candidate_zones(self):
        """Buffer Zone plus wherever the Pick route actually sources
        from (Main) — the two places a pallet can legitimately be
        placed. Includes each zone itself, not just its children, since
        a product with no dedicated sub-bin yet may still be tracked
        directly at the flat zone."""
        zones = self.env["stock.location"]
        buffer_zone = self.env["stock.location"].search([("name", "=", "Buffer Zone")], limit=1)
        if buffer_zone:
            zones |= buffer_zone
        warehouse = self.env["stock.warehouse"].search([], limit=1)
        if warehouse:
            pick_rule = self.env["stock.rule"].search(
                [("picking_type_id", "=", warehouse.pick_type_id.id)], limit=1
            )
            main_zone = pick_rule.location_src_id or warehouse.lot_stock_id
            if main_zone:
                zones |= main_zone
        return zones

    def action_confirm(self):
        self.ensure_one()
        record = self.env["clearance.bin.stock"]._get_or_create(
            self.product_id, self.location_id
        )
        if self.mode == "add":
            record.add_quantity(self.quantity, note=self.note)
            verb = "Placed"
        else:
            record.remove_quantity(self.quantity, note=self.note)
            verb = "Removed"
        # action_confirm previously just closed the popup with no visible
        # feedback at all — indistinguishable from silently doing nothing.
        # A notification on top of the close makes success actually
        # visible.
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Bin stock updated",
                "message": (
                    f"{verb} {self.quantity} × {self.product_id.display_name} "
                    f"at {self.location_id.display_name}. Now tracking "
                    f"{record.quantity} there."
                ),
                "type": "success",
                "next": {"type": "ir.actions.act_window_close"},
            },
        }
