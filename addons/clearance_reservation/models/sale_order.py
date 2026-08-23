from datetime import datetime, timedelta

from dateutil.relativedelta import relativedelta

from odoo import models, fields, api
from odoo.exceptions import AccessError

GRACE_PERIOD_DAYS = 15
FAR_FUTURE_MONTHS = 6
# A line holding stock qualifies to give it up in favor of an
# earlier-scheduled competitor once its own scheduled date sits at least
# this many days past a committed future incoming shipment that's been
# verified to actually cover its demand (see sale_order_line.py's
# _has_safe_future_replacement) — a real safety margin, not a hair's
# breadth.
SCHEDULED_FUTURE_STOCK_RELEASE_BUFFER_DAYS = 14
# The future incoming shipment matched against a line for that safety
# check is the one nearest to (scheduled_date + this many days) — a
# small head start before the order actually needs the goods, so the
# match isn't cutting it exactly to the wire.
SCHEDULED_FUTURE_STOCK_MATCH_BUFFER_DAYS = 10


class SaleOrder(models.Model):
    _inherit = "sale.order"

    fulfillment_stage = fields.Selection(
        [
            ("no_invoice", "No Payment"),
            ("grace_period", "Grace Period"),
            ("order_pick", "Order / Pick"),
            ("ship", "Ship"),
        ],
        default="no_invoice",
        copy=False,
        tracking=True,
    )
    # Purely a display refinement — the technical "no_invoice" value and
    # every domain/eligibility check in this module still use it exactly
    # as before; creating an invoice never touches fulfillment_stage and
    # must never trigger any reservation/allocation on its own. The
    # technical stage covers two different real situations that used to
    # share one misleading label: genuinely never invoiced at all ("No
    # Invoice"), or invoiced but not currently paid — including a fully
    # refunded order, which obviously DOES have an invoice ("No
    # Payment"). Computed fresh from current invoice_ids, never stored.
    fulfillment_stage_label = fields.Char(compute="_compute_fulfillment_stage_label")
    # Not field-level readonly: protection comes entirely from write()'s own
    # permission gate below (same pattern as fulfillment_stage), so that a
    # dedicated, explicitly-editable "Override Clearance Timestamp" field
    # can expose it in the view without a blanket readonly blocking it.
    clearance_date = fields.Datetime(copy=False)
    # Set only when clearance_date is cleared by a full-refund demotion to
    # no_invoice (see _demote_from_ship_if_unpaid) — preserves the
    # order's original place in line so getting paid again, or a manual
    # override, restores it exactly rather than sending the order to the
    # back as if it were brand new. Cleared once actually restored.
    clearance_date_backup = fields.Datetime(copy=False)
    # True whenever the CURRENT clearance_date came from a manual stage
    # override rather than genuine payment or grace-period confirmation.
    # The order still competes and validates exactly as if it had really
    # cleared (that's the whole point of the override) — this only flags
    # it for display, so the Pick/Ship badge and the forecast can avoid
    # showing a fabricated payment timestamp as if it were real. Reset to
    # False the moment a genuine payment event (re-)stamps clearance_date,
    # regardless of how it got set before.
    clearance_is_override = fields.Boolean(copy=False)
    is_reservation_hard_locked = fields.Boolean(
        string="Lock Reservations",
        copy=False,
        help="Once stock is reserved for this order, it can never be unreserved "
             "by any process, including manual overrides.",
    )
    # Stamped the moment is_reservation_hard_locked actually flips to True
    # (write()'s own bookkeeping below), cleared when it flips back to
    # False. A hard lock has no clearance_date to sort by — this gives it a
    # fair, non-arbitrary tiebreak against other hard-locked orders/lines:
    # whoever's override was applied first is served first, instead of
    # whatever order Postgres happens to return ties in (previously id asc).
    hard_lock_date = fields.Datetime(copy=False)
    # Two fields, sorted together ("queue_priority_bucket asc,
    # effective_queue_date asc"), deliberately kept separate rather than
    # folded into one Datetime — timestamps from different tiers must never
    # interleave directly against each other on the same clock; the bucket
    # is what actually decides precedence, the timestamp only breaks ties
    # WITHIN a bucket. Three tiers, by explicit product decision: hard lock
    # is the strongest override and always gets first claim on newly
    # available stock (bucket 0); force-reserve is a real but weaker
    # override, ranked below hard lock but still ahead of ordinary payment
    # (bucket 1); genuine payment/grace-period queuing is last of the three
    # for NEW stock (bucket 2) — it only ever competes for what neither
    # override tier needed. This ranks who gets stock that ISN'T already
    # spoken for; it has no bearing on stock ALREADY held — that's the
    # separate, unconditional "protected" guarantee in
    # _reserve_by_clearance, which nothing in any tier can ever reclaim. An
    # order that happens to be BOTH hard-locked and genuinely paid still
    # lands in bucket 0 — the lock is a trump card, not diluted by the
    # order also being paid.
    queue_priority_bucket = fields.Integer(
        compute="_compute_effective_queue_date", store=True, copy=False,
    )
    effective_queue_date = fields.Datetime(
        compute="_compute_effective_queue_date", store=True, copy=False,
    )
    # UI-only guard (not stored — a live judgment of current move state, not
    # something ever searched/sorted on): freezes the hard-lock toggle once
    # there is nothing left at the Stock level to hold or release. An open
    # backorder means some quantity is still unpicked, so the lock stays
    # meaningful (and editable) until every Pick-step picking for this order
    # — the original AND any backorder — has actually completed.
    pick_fully_validated = fields.Boolean(compute="_compute_pick_fully_validated")
    # Mirrors the Pick transfer's own Scheduled Date directly — not
    # sale_stock's native expected_date, which is a computed lead-time
    # promise rather than the transfer's actual, possibly manually
    # adjusted, scheduling. Editable here too, via the inverse below —
    # writes straight through to the Pick transfer itself rather than
    # keeping a separate value, so the two can never drift apart.
    pick_scheduled_date = fields.Datetime(
        string="Scheduled Date", compute="_compute_pick_scheduled_date",
        inverse="_inverse_pick_scheduled_date",
    )

    def _fulfillment_stage_label_for(self, stage):
        """The label to actually show for a given raw fulfillment_stage
        value — single source of truth used by the compute below AND by
        every chatter message that names a stage, so the two can never
        drift out of sync. Never used for eligibility/domain logic
        anywhere in this module — purely what gets displayed."""
        self.ensure_one()
        if stage == "no_invoice":
            return "No Payment" if self.invoice_ids else "No Invoice"
        return dict(self._fields["fulfillment_stage"].selection).get(stage, stage)

    @api.depends("fulfillment_stage", "invoice_ids")
    def _compute_fulfillment_stage_label(self):
        for order in self:
            order.fulfillment_stage_label = order._fulfillment_stage_label_for(order.fulfillment_stage)

    @api.depends("picking_ids.state", "picking_ids.picking_type_id")
    def _compute_pick_fully_validated(self):
        for order in self:
            pick_type = order.warehouse_id.pick_type_id
            pick_pickings = order.picking_ids.filtered(lambda p: p.picking_type_id == pick_type)
            order.pick_fully_validated = bool(pick_pickings) and all(
                p.state in ("done", "cancel") for p in pick_pickings
            ) and any(p.state == "done" for p in pick_pickings)

    @api.depends("picking_ids.scheduled_date", "picking_ids.picking_type_id")
    def _compute_pick_scheduled_date(self):
        for order in self:
            pick_type = order.warehouse_id.pick_type_id
            pick = order.picking_ids.filtered(lambda p: p.picking_type_id == pick_type)[:1]
            order.pick_scheduled_date = pick.scheduled_date if pick else False

    def _inverse_pick_scheduled_date(self):
        for order in self:
            if not order.pick_scheduled_date:
                continue
            pick_type = order.warehouse_id.pick_type_id
            pick = order.picking_ids.filtered(
                lambda p: p.picking_type_id == pick_type and p.state not in ("done", "cancel")
            )
            if pick:
                pick.write({"scheduled_date": order.pick_scheduled_date})

    @api.depends(
        "clearance_date", "hard_lock_date", "is_reservation_hard_locked",
        "order_line.is_reservation_hard_locked", "order_line.hard_lock_date",
        "order_line.is_force_reserved", "order_line.force_reserved_date",
    )
    def _compute_effective_queue_date(self):
        for order in self:
            hard_locked_lines = order.order_line.filtered("is_reservation_hard_locked")
            if order.is_reservation_hard_locked or hard_locked_lines:
                order.queue_priority_bucket = 0
                dates = list(filter(None, hard_locked_lines.mapped("hard_lock_date")))
                if order.is_reservation_hard_locked and order.hard_lock_date:
                    dates.append(order.hard_lock_date)
                order.effective_queue_date = min(dates) if dates else False
                continue
            force_reserved_lines = order.order_line.filtered("is_force_reserved")
            if force_reserved_lines:
                order.queue_priority_bucket = 1
                dates = list(filter(None, force_reserved_lines.mapped("force_reserved_date")))
                order.effective_queue_date = min(dates) if dates else False
                continue
            if order.clearance_date:
                order.queue_priority_bucket = 2
                order.effective_queue_date = order.clearance_date
            else:
                order.queue_priority_bucket = 3
                order.effective_queue_date = False

    def write(self, vals):
        # Clicking the fulfillment_stage badge, or editing the "Override
        # Clearance Timestamp" field, writes directly like any other
        # editable field — this intercepts both paths to gate them behind
        # the same override group as the module's other manual bypasses.
        # Our own automatic transitions (payment hook, cron promotion, and
        # this method's own clearance_date bookkeeping just below) pass
        # clearance_internal_write=True to skip both the gate and this
        # permission check, since they're not user-initiated overrides.
        manual_override = bool(
            {"fulfillment_stage", "clearance_date"} & vals.keys()
        ) and not self.env.context.get("clearance_internal_write")

        # Snapshot BEFORE super().write() — only orders whose value actually
        # flips get treated as a real transition; a redundant write of the
        # same value (e.g. re-saving a form) must never bump an order to the
        # back of the locked-queue, or re-run release cleanup, for no
        # reason. Locking itself stays ungated (same as every other
        # hard-lock entry point in this module), but RELEASING one needs the
        # same override permission as the other manual bypasses — it's what
        # used to be action_force_unlock_hard_lock's own gate, folded in
        # here so the boolean_toggle slider alone is enough to both lock
        # and release, with no separate button required.
        newly_locked = newly_unlocked = self.env["sale.order"]
        if "is_reservation_hard_locked" in vals:
            if vals["is_reservation_hard_locked"]:
                newly_locked = self.filtered(lambda o: not o.is_reservation_hard_locked)
            else:
                newly_unlocked = self.filtered(lambda o: o.is_reservation_hard_locked)

        releasing_hard_lock = bool(newly_unlocked) and not self.env.context.get(
            "clearance_internal_write"
        )
        if (manual_override or releasing_hard_lock) and not self.env.user.has_group(
            "clearance_reservation.group_reservation_override"
        ):
            raise AccessError(
                "You don't have permission to manually override the fulfillment "
                "stage, clearance timestamp, or release a reservation hard lock."
            )

        res = super().write(vals)

        if newly_locked:
            newly_locked.with_context(clearance_internal_write=True).write(
                {"hard_lock_date": fields.Datetime.now()}
            )
            for order in newly_locked:
                order.message_post(body=(
                    "🔒 Reservation hard lock enabled — whatever stock this order "
                    "holds, now or gained later, can never be unreserved by any "
                    "process (including manual overrides) until the lock is "
                    "released."
                ))
        if newly_unlocked:
            newly_unlocked.with_context(clearance_internal_write=True).write(
                {"hard_lock_date": False}
            )
            for order in newly_unlocked:
                order.message_post(body=(
                    "🔓 Reservation hard lock released — its reservation is open "
                    "to the normal clearance queue again and can be reallocated "
                    "if a higher-priority order needs it."
                ))
            # Clear the move-level is_locked_reservation flag the hard lock
            # itself applied — otherwise a move stays soft-locked forever
            # even after the hard lock is gone, since that flag is what
            # _do_unreserve actually checks. Deliberately NOT scoped to just
            # Pick-type moves: the hard lock's own flagging step (in
            # _reserve_by_clearance) applies to every open move across the
            # order, Ship leg included, once its origin Pick has completed —
            # Output-level stock is still genuinely contested between
            # multiple orders whose Picks are already done, so the lock
            # legitimately protects that leg too, and release must clear it
            # there as well. Never touches a line the user independently
            # force-reserved or hard-locked on its own — those are separate
            # protections released only through their own path.
            for order in newly_unlocked:
                locked_moves = order.order_line.move_ids.filtered(
                    lambda m: m.is_locked_reservation
                    and not m.sale_line_id.is_force_reserved
                    and not m.sale_line_id.is_reservation_hard_locked
                )
                locked_moves.write({"is_locked_reservation": False})

        if manual_override and "fulfillment_stage" in vals:
            new_stage = vals["fulfillment_stage"]
            stage_label = dict(self._fields["fulfillment_stage"].selection).get(new_stage, new_stage)
            if new_stage == "no_invoice":
                # Back up rather than just discard — an admin overriding
                # an order back to no_invoice shouldn't cost it its
                # original place in line if it (or a genuine payment
                # event) later brings it back into the queue. See
                # _resolve_clearance_date.
                for order in self.filtered("clearance_date"):
                    backed_up = order.clearance_date
                    # Computed per order, BEFORE the write below, since
                    # whether this order has any invoices decides "No
                    # Invoice" vs "No Payment" — not the same for every
                    # order in a batch override, unlike the other stages.
                    order_stage_label = order._fulfillment_stage_label_for("no_invoice")
                    order.with_context(clearance_internal_write=True).write({
                        "clearance_date": False,
                        "clearance_date_backup": order.clearance_date,
                        "clearance_is_override": False,
                    })
                    order.message_post(body=(
                        f"Manual override: moved to {order_stage_label}. Clearance "
                        f"timestamp ({backed_up}) backed up rather than discarded — "
                        f"restored automatically if payment resumes or it's "
                        f"overridden forward again."
                    ))
            elif new_stage in ("grace_period", "order_pick", "ship"):
                # The override DOES stamp a timestamp — the whole point is
                # that the order acts exactly as if it had genuinely
                # cleared (competes at its rightful tier, can validate a
                # Pick/Ship transfer). Flagged as clearance_is_override
                # purely for display (see stock_picking.py /
                # stock_forecasted.py), so a fabricated payment timestamp
                # is never shown as if it were real. The moment genuine
                # payment actually arrives, account_move.py's payment
                # hook replaces this with the real timestamp and clears
                # the flag — an override is a stand-in, never permanent.
                needing_clearance = self.filtered(lambda o: not o.clearance_date)
                if needing_clearance:
                    fresh_timestamp = fields.Datetime.now()
                    for order in needing_clearance:
                        resolved = order._resolve_clearance_date(fresh_timestamp)
                        restoring_backup = bool(order.clearance_date_backup)
                        order.with_context(clearance_internal_write=True).write({
                            "clearance_date": resolved,
                            "clearance_date_backup": False,
                            "clearance_is_override": True,
                        })
                        if restoring_backup:
                            order.message_post(body=(
                                f"Manual override: moved to {stage_label}, "
                                f"restoring its original clearance timestamp "
                                f"({resolved}) from before it lost payment — not a "
                                f"fresh stamp."
                            ))
                        else:
                            order.message_post(body=(
                                f"Manual override: moved to {stage_label}. "
                                f"Stamped a provisional clearance timestamp "
                                f"({resolved}), flagged as an override — competes "
                                f"and validates normally, but real payment will "
                                f"replace this timestamp the moment it arrives."
                            ))
                already_cleared = self - needing_clearance
                for order in already_cleared:
                    order.message_post(body=(
                        f"Manual override: moved to {stage_label} "
                        f"(already had a clearance timestamp — unchanged)."
                    ))

        # Re-run the queue for the affected product(s) immediately whenever
        # the stage OR the clearance timestamp itself changes — manual
        # override, payment arriving/reversing (which now promotes/demotes
        # Ship directly from account_move.py, then lands here too, so the
        # reservation attempt actually follows through), grace_period
        # expiring, or an admin directly re-timestamping an order to
        # change its place in line, or hard-locking/unlocking it — a
        # freshly hard-locked order should attempt to actually claim its
        # stock right away rather than waiting for some unrelated event to
        # eventually touch the same product. The clearance_date half is
        # only counted when it's NOT part of this method's own internal
        # bookkeeping just above (which already shares one trigger with
        # its accompanying fulfillment_stage write) — otherwise a single
        # manual stage override would fire this twice. Guarded on
        # _within_reserve_by_clearance purely so this can never recurse
        # into itself mid-run — _reserve_by_clearance no longer changes
        # fulfillment_stage on its own account at all (see the end of that
        # method), so in practice this guard now only matters for its own
        # recursive call chain, not for promote/demote anymore.
        trigger_on_clearance = "clearance_date" in vals and not self.env.context.get(
            "clearance_internal_write"
        )
        if (
            ("fulfillment_stage" in vals or trigger_on_clearance or "is_reservation_hard_locked" in vals)
            and not self.env.context.get("_within_reserve_by_clearance")
        ):
            product_ids = self.order_line.product_id.ids
            if product_ids:
                self.env["sale.order"]._reserve_by_clearance(product_ids=product_ids)
        return res

    def action_confirm(self):
        res = super().action_confirm()
        # A freshly-confirmed order gets a clearance timestamp and competes
        # for stock immediately, exactly like a paid order — but only for
        # GRACE_PERIOD_DAYS. _cron_expire_grace_period demotes it back to
        # no_invoice (and reclaims whatever it was holding) if no real
        # payment has arrived by then. Guarded on still being no_invoice so
        # this never fires twice or clobbers an order already paid before
        # confirmation.
        #
        # Stamped from the actual moment of confirmation, not create_date —
        # explicit product decision, reversed from an earlier design: a
        # quotation that sat around for a while before being confirmed
        # gets its place in line from when it was ACTUALLY confirmed, not
        # from when it was first drafted. If it goes on to actually get
        # paid while still in grace_period, this timestamp is what
        # "graduating" below deliberately leaves untouched.
        fresh = self.filtered(lambda o: o.fulfillment_stage == "no_invoice")
        for order in fresh:
            now = fields.Datetime.now()
            order.with_context(clearance_internal_write=True).write({
                "fulfillment_stage": "grace_period",
                "clearance_date": now,
                "clearance_is_override": False,
            })
            order.message_post(body=(
                f"Order confirmed — entered the Grace Period queue with clearance "
                f"timestamp {now} (the moment of confirmation). Competes for "
                f"stock for {GRACE_PERIOD_DAYS} days; demoted back to No Invoice "
                f"if it isn't genuinely paid by then."
            ))
        return res

    @api.model
    def _cron_expire_grace_period(self):
        cutoff = fields.Datetime.now() - timedelta(days=GRACE_PERIOD_DAYS)
        expired = self.search([
            ("fulfillment_stage", "=", "grace_period"),
            ("clearance_date", "<=", cutoff),
        ])
        if not expired:
            return
        # Demoting here writes fulfillment_stage, which write()'s own hook
        # picks up to immediately reclaim whatever these orders were
        # holding for the real queue — no separate call needed.
        # clearance_date_backup deliberately NOT set (unlike a full-refund
        # demotion) — losing the grace-period window means starting fresh
        # if it's ever genuinely paid later, not resuming its old spot.
        # Also explicitly cleared here in case an unrelated earlier event
        # left one behind — a stale backup must never resurrect an old
        # timestamp once the order has legitimately expired.
        for order in expired:
            order.message_post(body=(
                f"Grace period expired ({GRACE_PERIOD_DAYS} days unpaid since "
                f"{order.clearance_date}) — demoted to "
                f"{order._fulfillment_stage_label_for('no_invoice')}. Clearance "
                f"timestamp discarded with no backup kept; a later genuine "
                f"payment starts fresh, not from this timestamp."
            ))
        expired.with_context(clearance_internal_write=True).write({
            "fulfillment_stage": "no_invoice",
            "clearance_date": False,
            "clearance_date_backup": False,
            "clearance_is_override": False,
        })

    def _is_fully_paid(self):
        """Ship depends on payment status alone — an order doesn't need its
        Pick step (or anything else) fulfilled to ship; an incomplete order
        is still allowed to ship whatever it does have."""
        self.ensure_one()
        invoices = self.invoice_ids.filtered(
            lambda m: m.move_type == "out_invoice" and m.state == "posted"
        )
        if not invoices or not all(inv.payment_state == "paid" for inv in invoices):
            return False
        # A settled refund (credit note) reverses that payment — money
        # going back to the customer means the order is no longer fully
        # paid, regardless of what its original invoice(s) still show
        # (Odoo doesn't retroactively flip an already-paid invoice's own
        # payment_state just because a separately-paid refund exists
        # against it). Compared by NET amount rather than "any settled
        # refund at all", so a fresh, later invoice/payment cycle that
        # outweighs an old, already-accounted-for refund is correctly
        # fully paid again — not permanently disqualified by history.
        refunds = self.invoice_ids.filtered(
            lambda m: m.move_type == "out_refund" and m.state == "posted"
        )
        fully_settled_refunds = refunds.filtered(lambda r: r.payment_state == "paid")
        if not fully_settled_refunds:
            return True
        refunded_total = sum(fully_settled_refunds.mapped("amount_total"))
        paid_total = sum(invoices.mapped("amount_total"))
        return refunded_total < paid_total

    def _has_active_payment(self):
        """Best-effort read of whether ANY money is genuinely still paid on
        this order right now (as opposed to _is_fully_paid, which asks
        whether ALL of it still is). Used to decide how far a demotion
        should go: order_pick if something is still paid (a partial
        refund), all the way to no_invoice if nothing is (a full refund).
        """
        self.ensure_one()
        invoices = self.invoice_ids.filtered(
            lambda m: m.move_type == "out_invoice" and m.state == "posted"
        )
        if not any(inv.payment_state in ("partial", "in_payment", "paid") for inv in invoices):
            return False
        refunds = self.invoice_ids.filtered(
            lambda m: m.move_type == "out_refund" and m.state == "posted"
        )
        fully_settled_refunds = refunds.filtered(lambda r: r.payment_state == "paid")
        if not fully_settled_refunds:
            return True
        # A fully-settled refund exists — only still "active" if it
        # doesn't cover the full amount that was paid (a genuine partial
        # refund rather than a full one).
        refunded_total = sum(fully_settled_refunds.mapped("amount_total"))
        paid_total = sum(
            inv.amount_total for inv in invoices
            if inv.payment_state in ("partial", "in_payment", "paid")
        )
        return refunded_total < paid_total

    def _try_promote_to_ship(self):
        """Advance order_pick -> ship as soon as the order is fully paid."""
        for order in self:
            if order.fulfillment_stage == "order_pick" and order._is_fully_paid():
                order.with_context(clearance_internal_write=True).fulfillment_stage = "ship"
                order.message_post(body="Order fully paid — promoted from Order/Pick to Ship.")

    def _demote_from_ship_if_unpaid(self):
        """If a "ship" or "order_pick" order is no longer fully paid — e.g.
        a payment was reversed or a credit note settled the invoice back
        down — drop it to reflect its real payment state:

        - Still has SOME active payment (a partial refund): order_pick,
          clearance_date untouched — it keeps its original place in line
          rather than losing priority just because payment status changed.
        - Nothing is paid at all any more (a full refund): all the way
          back to no_invoice, since it has no more genuine claim on stock
          than any other unpaid order. clearance_date is backed up rather
          than just discarded — getting paid again, or a manual override,
          restores this order's ORIGINAL place in line instead of
          sending it to the back as if it were brand new (see
          _resolve_clearance_date).

        An order currently at ship/order_pick via a manual OVERRIDE
        (clearance_is_override) is deliberately exempt — it was never
        relying on genuine payment to be there in the first place, so a
        brand-new, not-yet-paid invoice simply existing (payment_state
        starts at "not_paid" for every invoice, the instant it's posted,
        long before anyone attempts to pay it) must never be mistaken for
        "payment was just reversed" and wipe out the override. Once real
        payment actually arrives and replaces the override (see
        account_move.py), the flag clears and normal demotion rules apply
        again from then on.
        """
        for order in self:
            if (
                order.fulfillment_stage not in ("ship", "order_pick")
                or order._is_fully_paid()
                or order.clearance_is_override
            ):
                continue
            if order._has_active_payment():
                if order.fulfillment_stage == "ship":
                    order.with_context(clearance_internal_write=True).fulfillment_stage = "order_pick"
                    order.message_post(body=(
                        "Payment partially reversed — demoted from Ship to "
                        "Order/Pick. Clearance timestamp untouched, keeps its "
                        "original place in line."
                    ))
            else:
                backed_up = order.clearance_date
                order.with_context(clearance_internal_write=True).write({
                    "fulfillment_stage": "no_invoice",
                    "clearance_date": False,
                    "clearance_date_backup": order.clearance_date,
                    "clearance_is_override": False,
                })
                order.message_post(body=(
                    f"Payment fully reversed — demoted to "
                    f"{order._fulfillment_stage_label_for('no_invoice')}, no "
                    f"genuine claim on stock left. Clearance timestamp "
                    f"({backed_up}) backed up, restored automatically if it's "
                    f"genuinely paid again."
                ))

    def _resolve_clearance_date(self, fresh_timestamp):
        """The timestamp to actually stamp when an order becomes eligible
        again — its backed-up ORIGINAL clearance_date if a full refund
        previously wiped it (see _demote_from_ship_if_unpaid above),
        otherwise the fresh timestamp for a genuinely new clearance."""
        self.ensure_one()
        return self.clearance_date_backup or fresh_timestamp

    def _ensure_buffer_replenishment(self, by_product):
        """Instantly top up Picking Zone from Buffer Zone for any product
        whose outstanding eligible demand exceeds what's currently free at
        Picking Zone, bounded by what Buffer actually has. Uses a real,
        immediately-validated stock.move (not a pending transfer left for
        someone to validate later) — the priority-ordered _action_assign
        loop right after this call always sees accurate, already-topped-up
        stock, never a "wait for a human" gap. The Pick move itself never
        sources from anywhere but Picking Zone, so this has no bearing on
        what the picking slip / Detailed Operations shows.

        Only ever moves what's genuinely free in Buffer — if Picking Zone
        and Buffer combined still can't cover demand, this does nothing
        and the shortfall is left exactly as it was: a real, honest wait
        for more stock to arrive.

        Every product needing a top-up in one pass is grouped into a
        SINGLE combined transfer (one per Buffer/Picking zone pair) —
        one document for a warehouse staffer to act on, not a separate
        one per product.
        """
        needs_by_zone_pair = {}
        for product_id, moves in by_product.items():
            outstanding = sum(
                max(0, m.product_uom_qty - m.quantity) for m in moves
            )
            if outstanding <= 0:
                continue
            # All moves for one product are assumed to share one warehouse
            # — this module has no multi-warehouse support anywhere else
            # either. Same warehouse-resolution fallback as stock_move.py's
            # total_warehouse_qty_available.
            first_move = moves[:1]
            warehouse = (
                first_move.picking_id.picking_type_id.warehouse_id
                or first_move.picking_type_id.warehouse_id
            )
            if not warehouse:
                continue
            picking_zone = warehouse._get_clearance_picking_zone()
            buffer_zone = warehouse._get_clearance_buffer_zone()
            if not picking_zone or not buffer_zone:
                continue
            product = self.env["product.product"].browse(product_id)
            picking_free = product.with_context(location=picking_zone.id).free_qty
            shortfall = outstanding - picking_free
            if shortfall <= 0:
                continue
            buffer_free = product.with_context(location=buffer_zone.id).free_qty
            qty = min(shortfall, buffer_free)
            if qty <= 0:
                continue
            needs_by_zone_pair.setdefault((buffer_zone.id, picking_zone.id), []).append((product, qty))

        for (buffer_zone_id, picking_zone_id), needs in needs_by_zone_pair.items():
            self._create_buffer_replenishment(
                needs,
                self.env["stock.location"].browse(buffer_zone_id),
                self.env["stock.location"].browse(picking_zone_id),
            )

    def _create_buffer_replenishment(self, needs, buffer_zone, picking_zone):
        """Create, reserve, and immediately validate ONE internal transfer
        moving every (product, qty) in `needs` from Buffer Zone to Picking
        Zone — a real stock.move per product, not raw SQL, so on-hand/
        reserved figures stay internally consistent (see stock_quant.py's
        move_quants override for the same principle applied to manual
        reorganization), all on one transfer document rather than one
        per product."""
        warehouse = buffer_zone.warehouse_id or picking_zone.warehouse_id
        internal_type = warehouse.int_type_id if warehouse else False
        picking_vals = {
            "location_id": buffer_zone.id,
            "location_dest_id": picking_zone.id,
            "origin": "Clearance Auto-Replenishment",
            "is_clearance_replenishment": True,
        }
        if internal_type:
            picking_vals["picking_type_id"] = internal_type.id
        picking = self.env["stock.picking"].create(picking_vals)
        moves = self.env["stock.move"]
        for product, qty in needs:
            moves |= self.env["stock.move"].create({
                "name": f"Auto-replenish {product.display_name}",
                "product_id": product.id,
                "product_uom_qty": qty,
                "product_uom": product.uom_id.id,
                "location_id": buffer_zone.id,
                "location_dest_id": picking_zone.id,
                "picking_id": picking.id,
            })
        moves._action_confirm()
        moves._action_assign()
        moves.move_line_ids.write({"picked": True})
        moves._action_done()
        detail = ", ".join(f"{qty} × {product.display_name}" for product, qty in needs)
        picking.message_post(body=(
            f"Auto-replenishment: moved {detail} from {buffer_zone.display_name} "
            f"to {picking_zone.display_name} to cover pending demand."
        ))
        return picking

    @api.model
    def _cron_reserve_by_clearance(self):
        self._reserve_by_clearance()

    @api.model
    def _reserve_by_clearance(self, product_ids=None):
        """Run the clearance queue, optionally scoped to a set of product
        (variant) ids. Scoping to specific products is safe without breaking
        the fairness guarantee: stock is reserved per product, so an order's
        turn for product A never competes with, or is affected by, another
        order's demand for product B. This lets a "reserve this product only"
        button reuse the exact same ordering logic as the full cron, just
        skipping the (irrelevant) work for every other product.
        """
        # This method's own two internal stage writes (promote-to-ship,
        # demote-from-ship, below) would otherwise re-trigger write()'s
        # "re-run the queue on any stage change" hook and recurse into this
        # same method mid-run. Tagging self here propagates the flag to
        # every recordset derived from it (orders, and everything reached
        # through relational traversal from orders) for the rest of this
        # call.
        self = self.with_context(_within_reserve_by_clearance=True)

        # "no_invoice" is otherwise never included: unpaid orders must never
        # compete for stock — hard lock is deliberately NOT an exception to
        # that. Hard lock grants no acquisition ability of its own at all:
        # its only function is protecting whatever an order already holds
        # from ever being unreserved (guaranteed unconditionally, and
        # independently of this domain, by the direct hard-lock field
        # checks in stock_move._do_unreserve/_action_cancel, and by hard
        # lock's exclusion from foreign_moves below) — a no_invoice order
        # that is ONLY hard-locked, with no genuine clearance and no
        # force-reserve of its own, is never attempted here and can never
        # acquire so much as one new unit through this method. It can only
        # ever hold stock that something else granted it (a native manual
        # reservation, or genuine payment gained later). is_force_reserved
        # IS an eligibility path here, unlike hard lock: force-reserve is
        # an active override that actually competes for stock (see
        # move_priority below), so a purely force-reserved no_invoice line
        # must still be attempted — action_force_reserve() only ever tries
        # _action_assign() once, at the moment it's clicked, and would
        # otherwise be invisible to every later retry (quant update,
        # incoming receipt, the safety-net cron) if it failed that first time.
        domain = [
            "|",
            ("fulfillment_stage", "in", ("order_pick", "ship", "grace_period")),
            ("order_line.is_force_reserved", "=", True),
        ]
        if product_ids:
            domain = ["&"] + domain + [("order_line.product_id", "in", product_ids)]
        # No ordering here — sale.order.queue_priority_bucket is an
        # order-wide aggregate that can be "contaminated" by a completely
        # unrelated line's override on a different product (e.g. an order
        # force-reserved on product A would wrongly cut in line for product
        # B too, ahead of an order genuinely entitled to priority on B).
        # Allocation below ranks each MOVE by its own line/order's actual
        # claim, per product — this recordset is just the set of orders
        # touched, for the promote/demote pass and the return count.
        orders = self.search(domain)

        move_domain = [
            ("state", "not in", ("done", "cancel")),
            ("sale_line_id", "!=", False),
            "|",
            ("sale_line_id.order_id.fulfillment_stage", "in", ("order_pick", "ship", "grace_period")),
            ("sale_line_id.is_force_reserved", "=", True),
        ]
        if product_ids:
            move_domain.append(("product_id", "in", product_ids))
        all_moves = self.env["stock.move"].search(move_domain)

        # Stock held by an unpaid (no_invoice) order has no legitimate claim
        # against the queue — the module's whole premise is that an unpaid
        # order never competes for stock. Such a reservation can only exist
        # via a path this module doesn't gate (the native forecast Reserve
        # link doesn't check payment status) or data older than this
        # module. Fold it into the same release pool as everything else so
        # it gets reclaimed for the queue — it's included here only to be
        # released, never reserved back, since it never joins `orders`.
        # Excludes hard-locked orders/lines AND force-reserved lines, for
        # two different reasons even though both are no_invoice too:
        # force-reserved lines are pulled into `orders` above to actively
        # compete and hold their own reservation; hard-locked ones are NOT
        # (see the domain above) but must still never be reclaimed here —
        # protecting whatever a hard lock already holds is unconditional
        # and is the one thing hard lock does regardless of eligibility.
        foreign_domain = [
            ("state", "in", ("assigned", "partially_available")),
            ("sale_line_id", "!=", False),
            ("sale_line_id.order_id.fulfillment_stage", "=", "no_invoice"),
            ("sale_line_id.order_id.is_reservation_hard_locked", "=", False),
            ("sale_line_id.is_reservation_hard_locked", "=", False),
            ("sale_line_id.is_force_reserved", "=", False),
        ]
        if product_ids:
            foreign_domain.append(("product_id", "in", product_ids))
        foreign_moves = self.env["stock.move"].search(foreign_domain)

        # An order scheduled more than FAR_FUTURE_MONTHS out has no
        # business holding scarce stock today — explicit product decision.
        # Computed up front (before the fast path below) since it affects
        # whether there's actually nothing to do: excluded from the
        # attempt loop entirely further down (by_product is built from
        # all_moves MINUS this set), so it never wins new stock, and
        # folded into the release step below like any other ordinary
        # reservation, so it doesn't keep what it had either. The one
        # exception: an order/line override (hard lock or force-reserve)
        # always sticks regardless of how far out the order is scheduled —
        # those are explicit admin overrides that bypass every other gate
        # in this module, and this is no different.
        far_future_cutoff = fields.Datetime.now() + relativedelta(months=FAR_FUTURE_MONTHS)
        far_future_moves = all_moves.filtered(
            lambda m: m.sale_line_id.order_id.pick_scheduled_date
            and m.sale_line_id.order_id.pick_scheduled_date > far_future_cutoff
            and not m.sale_line_id.order_id.is_reservation_hard_locked
            and not m.sale_line_id.is_reservation_hard_locked
            and not m.sale_line_id.is_force_reserved
        )

        # Fast path: if every move already sits at "assigned" — whether it
        # fairly won its place in the queue or is hard/soft-locked and
        # therefore untouchable anyway — a reallocation pass has nothing to
        # improve. No unpaid order is squatting on reclaimable stock, and
        # no far-future order is holding stock it shouldn't, either.
        # Skipping the unreserve-everything-then-reassign dance here is what
        # makes it safe for nearly every mutation in the system to trigger
        # this method without turning into O(competing orders) database
        # writes on every single event — most calls land here and do
        # nothing but a couple of cheap re-checks below.
        if (
            all_moves and all(m.state == "assigned" for m in all_moves)
            and not foreign_moves
            and not far_future_moves.filtered(lambda m: m.state == "assigned")
        ):
            return {"order_count": len(orders), "reserved_move_count": 0}

        touched = all_moves | foreign_moves
        before_state = {m.id: (m.state, m.quantity) for m in touched}

        # Protected from RELEASE, unconditionally — force-reserved moves
        # (via is_locked_reservation, set once one is actually granted),
        # and any move belonging to a hard-locked order or hard-locked
        # line (this mirrors _do_unreserve's own check exactly: calling
        # unreserve on any of these would raise there anyway). Everything
        # else — ordinary clearance-tier (payment) reservations, foreign
        # (ineligible, no_invoice-with-no-override) holds, and far-future
        # holds — gets released and reallocated strictly by move_priority
        # below on every run. That's what lets an order with an earlier
        # clearance_date reclaim stock a later-clearance order happened to
        # grab first — a payment-tier reservation that only ever gets
        # whatever was free at the moment it ran could never self-correct
        # an ordering mistake after the fact. Safe to release payment-tier
        # holds now that hard lock has no elevated priority of its own
        # (see move_priority) and no acquisition ability at all while
        # no_invoice (see the domain above) — reallocating can never
        # result in a hard lock claiming what it releases, since a
        # hard-locked order with no genuine clearance of its own isn't
        # even a candidate in the pass below.
        # "Scheduled Future Stock" holdings are ALSO excluded from this
        # blanket release — they must only ever be released to a
        # specifically earlier-SCHEDULED competitor for the same product
        # (see the targeted pass further below), never swept up by the
        # ordinary clearance_date-based reallocation here, which knows
        # nothing about scheduled dates and could otherwise hand their
        # stock to a merely earlier-CLEARANCE (but later-scheduled, or
        # unscheduled) order — not the guarantee this tag exists to make.
        protected = touched.filtered(
            lambda m: m.is_locked_reservation
            or m.sale_line_id.order_id.is_reservation_hard_locked
            or m.sale_line_id.is_reservation_hard_locked
            or m.sale_line_id.clearance_defer_reason == "Scheduled Future Stock"
        )
        releasable = (touched - protected).filtered(
            lambda m: m.state in ("assigned", "partially_available")
        )
        releasable._do_unreserve()

        all_moves = all_moves - far_future_moves

        # Ranks a MOVE by its own line/order's actual claim — never the
        # order-wide queue_priority_bucket (see the `orders` search above).
        # Grouped and processed per product below: fairness is a
        # per-product concept, an order's turn for product A must never be
        # decided by, or affect, its standing for product B.
        #
        # Hard lock is deliberately absent from this ranking — its only
        # job is protecting whatever an order ALREADY holds (guaranteed
        # unconditionally elsewhere: excluded from foreign_moves above, and
        # from _do_unreserve/_action_cancel directly), not winning it any
        # claim on stock that isn't already spoken for. A hard-locked order
        # with nothing paid or force-reserved of its own competes for new
        # stock at the very back, same as any other order with no real
        # claim — it's still attempted here (never invisible to the queue)
        # so it can pick up genuine leftovers, just never ahead of anyone
        # who actually has a claim.
        def move_priority(move):
            sale_line = move.sale_line_id
            order = sale_line.order_id
            # A line that gave up its stock under "Scheduled Future Stock"
            # (or would still qualify to) outranks EVERYTHING else,
            # force-reserve included, for whatever arrives next on this
            # product — force-reserve only ever takes stock that's
            # genuinely available; it must never be able to grab the very
            # future incoming shipment a holder gave up its current stock
            # specifically to reclaim, or the safety guarantee this whole
            # mechanism exists for would be broken by the next override
            # click. Ranked by its own scheduled_date, earliest first,
            # among each other.
            if sale_line.clearance_defer_reason == "Scheduled Future Stock":
                return (0, order.pick_scheduled_date or datetime.min)
            if sale_line.is_force_reserved:
                return (1, sale_line.force_reserved_date or datetime.min)
            if order.clearance_date:
                return (2, order.clearance_date)
            return (3, datetime.min)

        by_product = {}
        for m in all_moves:
            by_product.setdefault(m.product_id.id, self.env["stock.move"])
            by_product[m.product_id.id] |= m

        self._ensure_buffer_replenishment(by_product)

        reserved_moves = self.env["stock.move"]
        for product_moves in by_product.values():
            for move in product_moves.sorted(key=move_priority):
                # Anything not already fully assigned gets an attempt —
                # this covers both freshly-released moves and a protected
                # move that's merely partially_available and could still
                # gain more. Processed one at a time, strictly in priority
                # order, so each move only ever gets what's left after
                # every higher-priority move for the same product has
                # already taken its share.
                if move.state != "assigned":
                    move._action_assign()
                    if move.state == "assigned":
                        reserved_moves |= move
                # Flags every currently-assigned move eligible for a lock,
                # not just ones freshly assigned above — otherwise
                # hard-locking (or force-reserving) a line that was already
                # fully reserved beforehand (e.g. locked sometime after its
                # normal turn in the queue) would never actually get its
                # move-level protection applied. Also covers a
                # force-reserved line that failed to grab anything when
                # action_force_reserve() was first clicked and only just
                # succeeded here, via this method's own automatic retry.
                order = move.sale_line_id.order_id
                sale_line = move.sale_line_id
                if (
                    move.state in ("assigned", "partially_available")
                    and not move.is_locked_reservation
                    and (
                        order.is_reservation_hard_locked
                        or sale_line.is_reservation_hard_locked
                        or sale_line.is_force_reserved
                    )
                ):
                    move.write({"is_locked_reservation": True})

        # "Scheduled Future Stock" targeted release: a holder only ever
        # gives up its CURRENT stock to a specifically earlier-SCHEDULED
        # competitor for the same product — deliberately separate from
        # the main pass above (which only knows clearance_date, not
        # scheduled dates), and only reached for once every ordinary and
        # newly-freed avenue for that competitor is already exhausted.
        for product_moves in by_product.values():
            holders = product_moves.filtered(
                lambda m: m.sale_line_id.clearance_defer_reason == "Scheduled Future Stock"
            )
            if not holders:
                continue
            # Force-reserve is deliberately NOT an eligible demand here,
            # even with an earlier scheduled date — it only ever takes
            # stock that's genuinely available (see move_priority's tier
            # ordering above); it must never be able to trigger a
            # targeted release from a holder, which would hand it the
            # very shipment the holder gave up its stock to be promised
            # back, through this separate path.
            still_unfulfilled = (product_moves - holders).filtered(
                lambda m: m.state != "assigned" and not m.sale_line_id.is_force_reserved
            )
            for demand_move in still_unfulfilled.sorted(key=move_priority):
                demand_order = demand_move.sale_line_id.order_id
                if demand_move.state == "assigned" or not demand_order.pick_scheduled_date:
                    continue
                # Release from whichever eligible holder has the most
                # slack (latest scheduled_date) first, one at a time,
                # only as far as actually needed to satisfy this demand.
                eligible_holders = holders.filtered(
                    lambda m: m.state in ("assigned", "partially_available")
                    and m.sale_line_id.order_id.pick_scheduled_date
                    and m.sale_line_id.order_id.pick_scheduled_date > demand_order.pick_scheduled_date
                ).sorted(key=lambda m: m.sale_line_id.order_id.pick_scheduled_date, reverse=True)
                for holder in eligible_holders:
                    if demand_move.state == "assigned":
                        break
                    # Flagged the instant it actually gives up its stock —
                    # persists even once the move below holds nothing at
                    # all, so its elevated reclaim priority survives the
                    # gap until the promised future shipment arrives (see
                    # sale_order_line.py's is_scheduled_future_stock_release).
                    holder.sale_line_id.is_scheduled_future_stock_release = True
                    holder._do_unreserve()
                    demand_move._action_assign()
                if demand_move.state == "assigned":
                    reserved_moves |= demand_move
            # Give every touched holder a chance to reclaim whatever's
            # left over after the above, in its own elevated tier (see
            # move_priority) — it must never be stuck behind an
            # ordinary-priority order, OR a force-reserve, for the very
            # stock it's owed back.
            for holder in holders.sorted(key=move_priority):
                if holder.state != "assigned":
                    holder._action_assign()
                    if holder.state == "assigned":
                        reserved_moves |= holder
                sale_line = holder.sale_line_id
                if holder.state == "assigned":
                    # Fully reclaimed — the safety net has done its job,
                    # back to holding everything it's entitled to.
                    if sale_line.is_scheduled_future_stock_release:
                        sale_line.is_scheduled_future_stock_release = False
                elif (
                    sale_line.is_scheduled_future_stock_release
                    and not sale_line._has_safe_future_replacement()
                ):
                    # The promised future incoming shipment evaporated
                    # (e.g. the backing PO was cancelled or pushed out)
                    # after release — fall back to ordinary clearance-date
                    # priority rather than holding an elevated reclaim
                    # priority with nothing left actually backing it.
                    sale_line.is_scheduled_future_stock_release = False

        # Deliberately NOT calling _try_promote_to_ship/_demote_from_ship_if_unpaid
        # here (or anywhere else this method gets triggered from — quant
        # updates, receipts, lock toggles, the safety-net cron). Ship
        # depends on payment status alone, so the ONLY thing that should
        # ever move an order into or out of Ship is an actual payment
        # event — see account_move.py's _clearance_apply_payment_state.
        # Calling it here too would silently re-fight a manual Override
        # Stage write the instant it happened (this method runs as part of
        # that very write's own hook), and would demote a genuinely-earned
        # Ship stage over something that has nothing to do with payment at
        # all (e.g. an unrelated hard lock on a different product).

        changed = sum(
            1 for m in touched if before_state.get(m.id) != (m.state, m.quantity)
        )

        # One consolidated chatter note per affected PICKING per run,
        # rather than one post per move — a single event (a lock toggle,
        # a payment, a receipt) can ripple across several
        # lines/products at once, and a run touching dozens of orders
        # (e.g. the nightly cron) would otherwise spam each one's log
        # for no added clarity. Posted on the picking rather than the
        # sale order — this is physical stock movement, the picking's
        # own concern; lock/override/payment/stage events stay on the
        # order's chatter elsewhere in this module, since those are
        # about the order's business status, not warehouse operations.
        # Reuses move_priority's own tier ordering to explain WHY a
        # reservation was won, not just that it happened.
        tier_labels = {
            0: "reclaiming its earmarked future incoming shipment",
            1: "force-reserved",
            2: "clearance priority",
            3: "leftover stock, no real claim",
        }
        changed_moves = touched.filtered(
            lambda m: before_state.get(m.id) != (m.state, m.quantity)
        )
        by_target = {}
        for m in changed_moves:
            # Falls back to the order itself only if a move somehow has
            # no picking yet — shouldn't happen for a sale-order-driven
            # move, but never silently drop the information.
            target = m.picking_id or m.sale_line_id.order_id
            if not target:
                continue
            key = (target._name, target.id)
            by_target.setdefault(key, self.env["stock.move"])
            by_target[key] |= m
        for (model_name, target_id), moves in by_target.items():
            target = self.env[model_name].browse(target_id)
            lines = []
            for m in moves:
                before_qty = before_state.get(m.id, (None, 0))[1]
                after_qty = m.quantity
                product = m.product_id.display_name
                if after_qty > before_qty:
                    tier = tier_labels.get(move_priority(m)[0], "")
                    detail = f" ({tier})" if tier else ""
                    lines.append(f"reserved {after_qty - before_qty} × {product}{detail}")
                elif after_qty < before_qty:
                    lines.append(
                        f"released {before_qty - after_qty} × {product} "
                        f"(reallocated to a higher-priority claim)"
                    )
            if lines:
                target.message_post(body="Reservation queue: " + "; ".join(lines) + ".")

        return {"order_count": len(orders), "reserved_move_count": changed}
