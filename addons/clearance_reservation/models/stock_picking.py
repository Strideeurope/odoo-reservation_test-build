from odoo import models, fields
from odoo.exceptions import UserError


class StockPicking(models.Model):
    _inherit = "stock.picking"

    clearance_date = fields.Datetime(related="sale_id.clearance_date", string="Clearance Timestamp")
    clearance_is_override = fields.Boolean(related="sale_id.clearance_is_override")
    fulfillment_stage = fields.Selection(related="sale_id.fulfillment_stage", string="Fulfillment Stage")
    fulfillment_stage_label = fields.Char(related="sale_id.fulfillment_stage_label")
    # Summarizes across every line on this picking, most severe first — a
    # picking can have several lines with different lock states, but the
    # header badge only needs to flag the strongest one; the per-line badge
    # in the move list (stock.move.clearance_lock_reason directly) carries
    # the full detail.
    clearance_lock_reason = fields.Char(compute="_compute_clearance_lock_reason")
    # True only for a transfer sale_order.py generated on its own to
    # instantly top up Picking Zone from Buffer Zone (see
    # _ensure_buffer_replenishment) — never set on a picking a person
    # created themselves. Purely for traceability/filtering; nothing in
    # this module branches on it.
    is_clearance_replenishment = fields.Boolean(copy=False)

    def _compute_clearance_lock_reason(self):
        for picking in self:
            reasons = set(picking.move_ids.mapped("clearance_lock_reason"))
            if "Order Hard Lock" in reasons:
                picking.clearance_lock_reason = "Order Hard Lock"
            elif "Product Hard Lock" in reasons:
                picking.clearance_lock_reason = "Product Hard Lock"
            elif "Force Reserved" in reasons:
                picking.clearance_lock_reason = "Force Reserved"
            else:
                picking.clearance_lock_reason = False

    def button_validate(self):
        # No clearance timestamp means this order has never actually joined
        # the queue — not via payment, not even via a grace_period window.
        # A hard lock (or a line-level force-reserve) only earns the right
        # to HOLD stock in advance of that; it was never meant to authorize
        # a physical warehouse operation — Pick or Ship — to complete before
        # the order is genuinely queued. Checked ahead of, and regardless
        # of, the Ship-leg-specific check below: this blocks the Pick leg
        # too, which that check alone never did.
        for picking in self:
            order = picking.sale_id
            if order and not order.clearance_date:
                raise UserError(
                    f"Order {order.name} has no clearance timestamp — it has "
                    f"never been queued via payment or grace period. A hard "
                    f"lock or force-reserve only holds stock for it in "
                    f"advance; it does not authorize completing this "
                    f"transfer."
                )

        # Block the Output -> Customer step until the order is fully paid.
        # Disambiguated explicitly against the warehouse's Output location
        # rather than picking_type_id.code alone, since code=="outgoing" is
        # only guaranteed unique to the Ship leg for a 2-step (pick_ship)
        # route — a 3-step route or a custom operation type could reuse it.
        for picking in self:
            order = picking.sale_id
            if not order:
                continue
            output_loc = picking.picking_type_id.warehouse_id.wh_output_stock_loc_id
            is_ship_leg = (
                output_loc
                and picking.location_id.id == output_loc.id
                and picking.location_dest_id.usage == "customer"
            )
            if is_ship_leg and order.fulfillment_stage != "ship":
                raise UserError(
                    f"Order {order.name} is not fully paid yet — cannot ship "
                    f"until it reaches the Ship stage."
                )

        res = super().button_validate()

        # New stock arriving is the other natural "maybe a pending order can
        # now be satisfied" moment, alongside a manual inventory adjustment.
        # Gated on state == 'done': button_validate can instead return a
        # wizard action (immediate-transfer / backorder confirmation), in
        # which case nothing has actually been received yet and reservation
        # would just be working off stale quantities.
        incoming_done = self.filtered(
            lambda p: p.picking_type_id.code == "incoming" and p.state == "done"
        )
        if incoming_done:
            product_ids = incoming_done.move_ids.product_id.ids
            if product_ids:
                self.env["sale.order"]._reserve_by_clearance(product_ids=product_ids)

        return res
