from datetime import timedelta

from dateutil.relativedelta import relativedelta

from odoo import models, fields, api
from odoo.exceptions import AccessError

from .sale_order import (
    FAR_FUTURE_MONTHS,
    SCHEDULED_FUTURE_STOCK_MATCH_BUFFER_DAYS,
    SCHEDULED_FUTURE_STOCK_RELEASE_BUFFER_DAYS,
)


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    is_force_reserved = fields.Boolean(copy=False)
    # Mirrors hard_lock_date exactly, for the same reason: force-reserve is
    # the other admin override that bypasses the payment gate, and now
    # competes over time just like a hard lock (action_force_reserve can
    # fail on the spot for lack of stock and get picked up later by this
    # same automatic queue) — it needs the identical fair, chronological
    # tiebreak against other overrides, not an arbitrary one.
    force_reserved_date = fields.Datetime(copy=False)
    is_reservation_hard_locked = fields.Boolean(
        string="Product Hard Lock",
        copy=False,
        help="Same protection as the order-wide hard lock, but scoped to just "
             "this product on this order instead of the whole order: once "
             "stock is reserved for this line, it can never be unreserved by "
             "any process, including manual overrides, and it bypasses the "
             "payment gate to compete for stock immediately.",
    )
    # Mirrors sale_order.hard_lock_date exactly: stamped the moment this
    # line's own lock flips to True, cleared when it flips back to False.
    # sale_order._compute_effective_queue_date reads this (for an order
    # whose OWN is_reservation_hard_locked is False but has a locked line)
    # to give such an order a fair, chronological place in the hard-locked
    # queue instead of an arbitrary one.
    hard_lock_date = fields.Datetime(copy=False)
    # Mirrors sale_order.pick_fully_validated: freezes THIS line's own
    # hard-lock toggle once its Pick-step moves (original + any backorder)
    # are all done/cancelled with at least one actually done — an open
    # backorder for this specific line means there's still unpicked
    # quantity to hold, so it stays lockable until that's resolved too.
    pick_fully_validated = fields.Boolean(compute="_compute_pick_fully_validated")
    # Mirrors _reserve_by_clearance's own far_future_moves exclusion
    # exactly — surfaces WHY a line isn't (or won't be) getting stock,
    # right where the overrides that would exempt it (hard lock,
    # force-reserve) already live, instead of leaving that silent.
    clearance_defer_reason = fields.Char(compute="_compute_clearance_defer_reason")
    # Persistent (not derived from current move state) — flips True the
    # instant _reserve_by_clearance's targeted release pass actually makes
    # this line give up its held stock to an earlier-scheduled competitor,
    # and stays True even though the line then holds NOTHING at all, so
    # its elevated reclaim priority (see sale_order.py's move_priority)
    # survives across the gap between giving up its old stock and the
    # promised future shipment actually arriving. Without this, the tag
    # (and the priority tier it grants) would vanish the moment the line's
    # own moves left "assigned"/"partially_available" state — exactly the
    # instant an ordinary order, or a force-reserve, could otherwise cut
    # in and take the very shipment this mechanism exists to protect.
    # Cleared once the line reclaims enough stock to be fully assigned
    # again, or if the safety net backing it evaporates in the meantime
    # (see _reserve_by_clearance).
    is_scheduled_future_stock_release = fields.Boolean(copy=False)

    @api.depends("move_ids.state", "move_ids.picking_type_id")
    def _compute_pick_fully_validated(self):
        for line in self:
            pick_type = line.order_id.warehouse_id.pick_type_id
            pick_moves = line.move_ids.filtered(lambda m: m.picking_type_id == pick_type)
            line.pick_fully_validated = bool(pick_moves) and all(
                m.state in ("done", "cancel") for m in pick_moves
            ) and any(m.state == "done" for m in pick_moves)

    @api.depends(
        "order_id.pick_scheduled_date", "order_id.is_reservation_hard_locked",
        "order_id.fulfillment_stage",
        "is_reservation_hard_locked", "is_force_reserved",
        "is_scheduled_future_stock_release",
        "move_ids.state", "move_ids.product_uom_qty",
    )
    def _compute_clearance_defer_reason(self):
        cutoff = fields.Datetime.now() + relativedelta(months=FAR_FUTURE_MONTHS)
        for line in self:
            order = line.order_id
            overridden = (
                order.is_reservation_hard_locked
                or line.is_reservation_hard_locked
                or line.is_force_reserved
            )
            # A no_invoice order (not hard-locked, not force-reserved) has
            # no legitimate claim on stock at all — the module's whole
            # premise. Without this check, a line that happened to
            # qualify for "Scheduled Future Stock" while genuinely
            # eligible (e.g. in grace_period) would keep that tag even
            # after the order lost eligibility entirely (demoted via a
            # full refund, expired out of grace_period, or manually
            # overridden to no_invoice) — and since that tag PROTECTS a
            # line from the blanket reclaim in _reserve_by_clearance, an
            # order with zero real claim could indefinitely squat on
            # stock it's not entitled to, just by coincidentally matching
            # a future incoming shipment. Never true for a genuinely
            # ineligible order, so this must be checked before anything
            # else below, including the persistent release flag.
            ineligible = order.fulfillment_stage == "no_invoice" and not overridden
            if overridden or ineligible or not order.pick_scheduled_date:
                line.clearance_defer_reason = False
                continue
            if order.pick_scheduled_date > cutoff:
                line.clearance_defer_reason = "Scheduled Far Out"
                continue
            # Already released under this mechanism (possibly holding
            # nothing at all right now) — stays tagged, independent of
            # current move state, until it reclaims enough stock or the
            # safety net backing it evaporates (see _reserve_by_clearance).
            if line.is_scheduled_future_stock_release:
                line.clearance_defer_reason = "Scheduled Future Stock"
                continue
            held = line.move_ids.filtered(lambda m: m.state in ("assigned", "partially_available"))
            if held and line._has_safe_future_replacement():
                line.clearance_defer_reason = "Scheduled Future Stock"
            else:
                line.clearance_defer_reason = False

    def _get_committed_future_incoming_moves(self):
        """Incoming stock.move records for this line's product, at its own
        warehouse, sourced from a COMMITTED purchase order (state
        'purchase' or 'done' — never a draft/sent RFQ, which could still
        fall through and never actually arrive). This is deliberately a
        stricter bar than "any incoming move" — the whole point is a real
        safety guarantee, not a hopeful one."""
        self.ensure_one()
        return self.env["stock.move"].search([
            ("product_id", "=", self.product_id.id),
            ("picking_type_id", "=", self.order_id.warehouse_id.in_type_id.id),
            ("state", "not in", ("done", "cancel")),
            ("date", "!=", False),
            ("purchase_line_id.order_id.state", "in", ("purchase", "done")),
        ], order="date asc")

    def _has_safe_future_replacement(self):
        """True if a committed future incoming shipment, arriving with a
        genuine buffer before this line's own scheduled_date, can be
        trusted to cover its demand — the guarantee that makes releasing
        its CURRENT stock (to an earlier-scheduled competitor) safe rather
        than reckless. Matched to the shipment nearest
        scheduled_date + SCHEDULED_FUTURE_STOCK_MATCH_BUFFER_DAYS (on or
        before that target if possible), then gated on that shipment
        landing at least SCHEDULED_FUTURE_STOCK_RELEASE_BUFFER_DAYS ahead
        of the actual need, and confirmed by summing ALL committed
        incoming quantity up to that same date against this line's own
        demand — not a full simulation of every other order that might
        also want a share of it, but a real, live-rechecked read of
        whether the numbers actually work out.
        """
        self.ensure_one()
        order = self.order_id
        incoming = self._get_committed_future_incoming_moves()
        if not incoming:
            return False

        target_date = order.pick_scheduled_date + timedelta(days=SCHEDULED_FUTURE_STOCK_MATCH_BUFFER_DAYS)
        on_or_before = incoming.filtered(lambda m: m.date <= target_date)
        candidates = on_or_before or incoming
        nearest = min(candidates, key=lambda m: abs((m.date - target_date).total_seconds()))

        release_cutoff = nearest.date + timedelta(days=SCHEDULED_FUTURE_STOCK_RELEASE_BUFFER_DAYS)
        if order.pick_scheduled_date < release_cutoff:
            return False

        cumulative_qty = sum(incoming.filtered(lambda m: m.date <= nearest.date).mapped("product_uom_qty"))
        return cumulative_qty >= self.product_uom_qty

    def write(self, vals):
        # Snapshot BEFORE super().write() — only lines whose value actually
        # flips get treated as a real transition; a redundant write of the
        # same value must never bump a line to the back of the locked-queue,
        # or re-run release cleanup, for no reason. Locking stays ungated;
        # releasing needs the override permission — folded in here (used to
        # live only in action_force_unlock_hard_lock) so the boolean_toggle
        # slider alone is enough to both lock and release.
        newly_locked = newly_unlocked = self.env["sale.order.line"]
        if "is_reservation_hard_locked" in vals:
            if vals["is_reservation_hard_locked"]:
                newly_locked = self.filtered(lambda l: not l.is_reservation_hard_locked)
            else:
                newly_unlocked = self.filtered(lambda l: l.is_reservation_hard_locked)

        # Same before-super() snapshot pattern, for force_reserved_date.
        newly_force_reserved = newly_force_unreserved = self.env["sale.order.line"]
        if "is_force_reserved" in vals:
            if vals["is_force_reserved"]:
                newly_force_reserved = self.filtered(lambda l: not l.is_force_reserved)
            else:
                newly_force_unreserved = self.filtered(lambda l: l.is_force_reserved)

        if newly_unlocked and not self.env.user.has_group(
            "clearance_reservation.group_reservation_override"
        ):
            raise AccessError("You don't have permission to release a reservation hard lock.")
        if newly_force_unreserved and not self.env.user.has_group(
            "clearance_reservation.group_reservation_override"
        ):
            raise AccessError("You don't have permission to release a forced reservation.")

        res = super().write(vals)

        if newly_locked:
            newly_locked.write({"hard_lock_date": fields.Datetime.now()})
            for line in newly_locked:
                line.order_id.message_post(body=(
                    f"🔒 Product hard lock enabled on {line.product_id.display_name} "
                    f"— whatever stock this line holds, now or gained later, can "
                    f"never be unreserved by any process until the lock is "
                    f"released."
                ))
        if newly_unlocked:
            newly_unlocked.write({"hard_lock_date": False})
            # Clear the move-level lock the hard lock itself applied — but
            # never a lock the line independently picked up via
            # action_force_reserve, that's a separate protection released
            # only through its own unlock.
            for line in newly_unlocked:
                locked_moves = line.move_ids.filtered(
                    lambda m: m.is_locked_reservation and not line.is_force_reserved
                )
                locked_moves.write({"is_locked_reservation": False})
                line.order_id.message_post(body=(
                    f"🔓 Product hard lock released on {line.product_id.display_name} "
                    f"— open to the normal clearance queue again."
                ))

        if newly_force_reserved:
            newly_force_reserved.write({"force_reserved_date": fields.Datetime.now()})
            for line in newly_force_reserved:
                line.order_id.message_post(body=(
                    f"⚡ Force-reserve enabled on {line.product_id.display_name} "
                    f"— bypasses the clearance queue entirely and jumps genuine "
                    f"payment for stock that isn't already spoken for."
                ))
        if newly_force_unreserved:
            newly_force_unreserved.write({"force_reserved_date": False})
            for line in newly_force_unreserved:
                line.order_id.message_post(body=(
                    f"Force-reserve released on {line.product_id.display_name} "
                    f"— back to competing on genuine clearance priority alone."
                ))
            # Clear the move-level lock the force-reserve itself applied —
            # otherwise a move stays soft-locked forever even after the
            # override is gone, since that flag is what _do_unreserve
            # actually checks. Mirrors the newly_unlocked branch above;
            # never touches a lock the line independently holds via its own
            # hard lock, that's a separate protection released only through
            # its own unlock.
            for line in newly_force_unreserved:
                locked_moves = line.move_ids.filtered(
                    lambda m: m.is_locked_reservation and not line.is_reservation_hard_locked
                )
                locked_moves.write({"is_locked_reservation": False})
        # Increasing an already-confirmed line's quantity updates its move's
        # demand automatically (Odoo's own logic), but leaves the move
        # sitting at whatever it already had reserved — nothing else
        # re-attempts reservation for the new shortfall on its own. Closes
        # that gap the same way every other mutation path does: re-run the
        # queue immediately for the affected product, scoped to orders
        # actually in it.
        if "product_uom_qty" in vals and not self.env.context.get(
            "_within_reserve_by_clearance"
        ):
            queued_lines = self.filtered(
                lambda l: l.order_id.fulfillment_stage in ("order_pick", "ship", "grace_period")
            )
            product_ids = queued_lines.product_id.ids
            if product_ids:
                self.env["sale.order"]._reserve_by_clearance(product_ids=product_ids)

        # Mirrors sale_order.write()'s own hook for is_reservation_hard_locked:
        # flipping this on should attempt to actually claim the line's stock
        # right away rather than waiting for some unrelated event to touch
        # the same product; flipping it off should let the queue immediately
        # reclaim it if a higher-priority order is waiting.
        if "is_reservation_hard_locked" in vals and not self.env.context.get(
            "_within_reserve_by_clearance"
        ):
            product_ids = self.product_id.ids
            if product_ids:
                self.env["sale.order"]._reserve_by_clearance(product_ids=product_ids)

        # Same reasoning as the hard-lock hook above: releasing a forced
        # reservation should let the queue immediately reclaim it for
        # whoever's actually next in line, rather than leaving it idle
        # until some unrelated event happens to touch the same product.
        if "is_force_reserved" in vals and not self.env.context.get(
            "_within_reserve_by_clearance"
        ):
            product_ids = self.product_id.ids
            if product_ids:
                self.env["sale.order"]._reserve_by_clearance(product_ids=product_ids)
        return res

    def action_force_reserve(self):
        """Bypass the clearance queue entirely for this line's stock moves.
        Works regardless of the order's fulfillment_stage."""
        for line in self:
            unassigned = line.move_ids.filtered(
                lambda m: m.state in ("confirmed", "waiting", "partially_available")
            )
            # _skip_auto_reserve_trigger: the lock (assigned.write below)
            # applies a moment AFTER this call, not before — without this
            # flag, stock.move._action_assign's own auto-rebalance hook
            # could see this reservation as still-unprotected and unreserve
            # it again before the lock ever gets a chance to apply.
            unassigned.with_context(_skip_auto_reserve_trigger=True)._action_assign()
            unassigned.invalidate_recordset(["state"])
            # Checked against ALL the line's moves, not just the
            # previously-unassigned subset above: a line with abundant,
            # uncontested stock is often ALREADY "assigned" the moment the
            # order is confirmed, before anyone ever clicks this button —
            # narrowing to `unassigned` here would silently skip locking
            # (and flagging is_force_reserved) that move entirely, reporting
            # "nothing force-reserved" for a line that in fact holds stock.
            assigned = line.move_ids.filtered(lambda m: m.state == "assigned")
            assigned.write({"is_locked_reservation": True})
            line.is_force_reserved = bool(assigned)

    def action_force_unlock_reservation(self):
        self.ensure_one()
        if not self.env.user.has_group("clearance_reservation.group_reservation_override"):
            raise AccessError("You don't have permission to unlock a forced reservation.")
        self.move_ids.write({"is_locked_reservation": False})
        self.is_force_reserved = False
