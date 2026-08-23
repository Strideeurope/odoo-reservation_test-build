from odoo import fields, models


class StockWarehouse(models.Model):
    _inherit = "stock.warehouse"

    # Left empty, the auto-replenishment feature in sale_order.py simply
    # never triggers — a shortfall waits exactly like it did before this
    # was configured. There's no equivalent field for Picking Zone: its
    # location is already fully determined by the Pick route's own
    # stock.rule (see _get_clearance_picking_zone), and duplicating that
    # as a separate field here would risk the two silently disagreeing.
    buffer_zone_location_id = fields.Many2one(
        "stock.location",
        string="Clearance Buffer Zone",
        help="Overstock location the clearance reservation engine can draw "
             "from to instantly top up the Picking Zone whenever pending "
             "demand exceeds what's currently there.",
    )

    def _get_clearance_picking_zone(self):
        """The Pick route's actual source location — read from its own
        stock.rule, since that's the one thing that actually determines
        what the Pick move can reserve from, not any operation-type
        default. Falls back to the warehouse's top-level Stock location
        if the rule has no explicit source (mirrors the lookup already
        established in tests/test_clearance_reservation.py)."""
        self.ensure_one()
        pick_rule = self.env["stock.rule"].search(
            [("picking_type_id", "=", self.pick_type_id.id)], limit=1
        )
        return pick_rule.location_src_id or self.lot_stock_id

    def _get_clearance_buffer_zone(self):
        self.ensure_one()
        return self.buffer_zone_location_id
