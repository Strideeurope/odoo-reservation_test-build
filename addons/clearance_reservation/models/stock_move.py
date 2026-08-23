from odoo import models, fields
from odoo.exceptions import UserError


class StockMove(models.Model):
    _inherit = "stock.move"

    is_locked_reservation = fields.Boolean(copy=False)
    # Shared by the Inventory forecast badge and the Pick/Ship picking form
    # badge — a single source of truth for "why (if at all) is this move's
    # reservation locked", so both surfaces can never drift out of sync
    # with each other.
    clearance_lock_reason = fields.Char(compute="_compute_clearance_lock_reason")
    # The Pick operation only ever reserves from (and shows) the Picking
    # Zone — a picker's slip legitimately shows 0 there even when the
    # product genuinely exists elsewhere in the warehouse (Buffer Zone),
    # since that stock hasn't been claimed by this move yet. Left as a
    # silent 0 with no explanation, that reads as "there's nothing to be
    # had" rather than "go find it in Buffer and bring it here first" —
    # this surfaces the true total so the picker knows there's something
    # to go get, without the slip itself ever exposing Buffer as a
    # distinct source location. Not stored: a live figure, recomputed on
    # every view render, same as any other on-hand quantity display.
    total_warehouse_qty_available = fields.Float(
        string="Total in Warehouse",
        compute="_compute_total_warehouse_qty_available",
    )

    def _compute_total_warehouse_qty_available(self):
        for move in self:
            warehouse = move.picking_id.picking_type_id.warehouse_id or move.picking_type_id.warehouse_id
            if not move.product_id or not warehouse:
                move.total_warehouse_qty_available = 0.0
                continue
            move.total_warehouse_qty_available = move.product_id.with_context(
                location=warehouse.lot_stock_id.id
            ).qty_available

    def _compute_clearance_lock_reason(self):
        for move in self:
            sale_line = move.sale_line_id
            if not sale_line:
                move.clearance_lock_reason = False
                continue
            order = sale_line.order_id
            if order.is_reservation_hard_locked:
                move.clearance_lock_reason = "Order Hard Lock"
            elif sale_line.is_reservation_hard_locked:
                move.clearance_lock_reason = "Product Hard Lock"
            elif sale_line.is_force_reserved:
                move.clearance_lock_reason = "Force Reserved"
            else:
                # Reuses sale_order_line's own far-future check rather than
                # re-deriving it a second time — keeps the wording (and the
                # exact cutoff logic) identical everywhere it's shown:
                # here (picking header badge, forecast), and on the order
                # line itself.
                defer_reason = sale_line.clearance_defer_reason
                # "Scheduled Future Stock" marks a line as ELIGIBLE to
                # give up its stock later (or still waiting to reclaim
                # what it already gave up) — that's meaningful internal
                # protection (see sale_order.py's move_priority/protected
                # set) regardless of current state, but as a user-facing
                # badge it should only appear while genuinely short right
                # now. A move sitting at "assigned" already holds its
                # full demand — nothing pending, nothing to flag — even
                # though the line underneath may still legitimately carry
                # the tag (and its protection) in case an earlier-
                # scheduled competitor needs it later.
                if defer_reason == "Scheduled Future Stock" and move.state == "assigned":
                    move.clearance_lock_reason = False
                else:
                    move.clearance_lock_reason = defer_reason or False

    def _do_unreserve(self):
        hard_locked = self.filtered(
            lambda m: m.sale_line_id.order_id.is_reservation_hard_locked
            or m.sale_line_id.is_reservation_hard_locked
        )
        if hard_locked:
            raise UserError(
                "These reservations belong to a hard-locked order (or a "
                "hard-locked product on it) and cannot be released under any "
                "circumstance: %s"
                % ", ".join(hard_locked.mapped("product_id.display_name"))
            )

        soft_locked = self.filtered("is_locked_reservation") - hard_locked
        if soft_locked and not self.env.context.get("force_unreserve_override"):
            raise UserError(
                "These reservations are locked and cannot be released: %s"
                % ", ".join(soft_locked.mapped("product_id.display_name"))
            )

        return super()._do_unreserve()

    def _action_cancel(self):
        locked = self.filtered(
            lambda m: m.is_locked_reservation
            or m.sale_line_id.order_id.is_reservation_hard_locked
            or m.sale_line_id.is_reservation_hard_locked
        )
        if locked and not self.env.context.get("force_unreserve_override"):
            raise UserError("Unlock the reservation before cancelling these moves.")
        return super()._action_cancel()

    def _action_done(self, cancel_backorder=False):
        res = super()._action_done(cancel_backorder=cancel_backorder)
        # Auto-release the *soft* (line-level) lock once the move actually ships.
        # Hard locks are left as-is — administrative closure only, see sale_order.py.
        res.filtered("is_locked_reservation").write({"is_locked_reservation": False})
        return res

    def _action_assign(self, force_qty=False):
        res = super()._action_assign(force_qty=force_qty)
        # Ship depends on payment status alone (see sale_order.py), so
        # reservation state has no bearing on stage promotion any more —
        # that's driven exclusively by account_move.py's payment hook now.
        orders = self.filtered(lambda m: m.state in ("assigned", "partially_available")).sale_line_id.order_id

        # Closes the last gap in "the queue is always self-correcting": the
        # native forecast "Reserve" link calls _action_assign directly,
        # completely bypassing _reserve_by_clearance, which can reserve
        # stock out of clearance-priority order with nothing to ever
        # correct it afterwards. Re-running the queue right here catches
        # that immediately. Skipped for our own internal per-order assign
        # calls inside _reserve_by_clearance (would recurse), and for
        # action_force_reserve (which locks a moment after this call — an
        # auto-rebalance running in that gap could unreserve it again
        # before the lock ever applies).
        if not self.env.context.get("_within_reserve_by_clearance") and not self.env.context.get(
            "_skip_auto_reserve_trigger"
        ):
            queued_orders = orders.filtered(
                lambda o: o.fulfillment_stage in ("order_pick", "ship", "grace_period")
            )
            product_ids = self.filtered(
                lambda m: m.sale_line_id.order_id in queued_orders
            ).product_id.ids
            if product_ids:
                self.env["sale.order"]._reserve_by_clearance(product_ids=product_ids)
        return res
