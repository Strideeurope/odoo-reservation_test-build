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
# _get_group_safe_future_replacement_lines) — a real safety margin, not a
# hair's breadth. Applies once the matched shipment's own PO is
# genuinely confirmed (purchase_order.py's is_receipt_confirmed —
# container reference AND port arrival date both on file): real
# logistics data, not just a plan, justifies trusting a tighter margin.
# The matched shipment's own move.date already reflects port arrival +
# transit time once confirmed — see purchase_order.py's
# _sync_confirmed_receipt_date — so this is measured against that same
# move.date/date_planned in both the confirmed and unconfirmed case.
SCHEDULED_FUTURE_STOCK_RELEASE_BUFFER_DAYS = 7
# The wider margin required when the matched shipment's PO is NOT yet
# confirmed — still just a committed order with a planned date, no
# container/port data locking the timing down. Explicit product
# decision: more room for a merely-planned date to slip before it's
# trusted enough to justify giving up real, currently-held stock.
SCHEDULED_FUTURE_STOCK_RELEASE_BUFFER_DAYS_UNCONFIRMED = 30
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
    # Set only when clearance_date is cleared by a MANUAL override back to
    # No Invoice (see write() below) — preserves the order's original
    # place in line so getting paid again, or a manual override forward,
    # restores it exactly rather than sending the order to the back as if
    # it were brand new. Cleared once actually restored. Losing payment
    # automatically (refund, cancelled/draft-reset invoice) no longer
    # touches clearance_date at all — see _demote_from_ship_if_unpaid.
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
    # Set by _demote_from_ship_if_unpaid whenever payment is lost with no
    # stage change to make (already at Order/Pick) — records WHY in a
    # filterable field, not just in the chatter, and doubles as a dedupe
    # guard: Odoo can recompute payment_state (and re-run this check)
    # several times within one transaction, and without something stored
    # to compare against, that would re-post the same chatter note every
    # time. Cleared the moment the order genuinely re-clears (real payment,
    # or a manual override forward).
    clearance_last_demotion_reason = fields.Char(copy=False)
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
        # reason. Both directions — applying AND releasing — need the same
        # override permission as the other manual bypasses — it's what used
        # to be action_force_unlock_hard_lock's own gate, folded in here so
        # the boolean_toggle slider alone is enough to both lock and
        # release, with no separate button required.
        newly_locked = newly_unlocked = self.env["sale.order"]
        if "is_reservation_hard_locked" in vals:
            if vals["is_reservation_hard_locked"]:
                newly_locked = self.filtered(lambda o: not o.is_reservation_hard_locked)
            else:
                newly_unlocked = self.filtered(lambda o: o.is_reservation_hard_locked)

        applying_hard_lock = bool(newly_locked) and not self.env.context.get(
            "clearance_internal_write"
        )
        releasing_hard_lock = bool(newly_unlocked) and not self.env.context.get(
            "clearance_internal_write"
        )
        if (
            manual_override or applying_hard_lock or releasing_hard_lock
        ) and not self.env.user.has_group("clearance_reservation.group_reservation_override"):
            raise AccessError(
                "You don't have permission to manually override the fulfillment "
                "stage, clearance timestamp, or apply/release a reservation hard lock."
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
            # order, Ship leg included, once its origin Pick has completed
            # — so release must clear it there as well. Never touches a
            # line the user independently force-reserved or hard-locked on
            # its own, or a move that's independently protected for having
            # genuinely reached Output (see _clearance_is_output_move) —
            # those are separate protections released only through their
            # own path (Output protection has no release path at all).
            for order in newly_unlocked:
                locked_moves = order.order_line.move_ids.filtered(
                    lambda m: m.is_locked_reservation
                    and not m.sale_line_id.is_force_reserved
                    and not m.sale_line_id.is_reservation_hard_locked
                    and not self._clearance_is_output_move(m)
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
                            "clearance_last_demotion_reason": False,
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
                already_cleared.with_context(clearance_internal_write=True).write({
                    "clearance_last_demotion_reason": False,
                })
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

    def _demote_from_ship_if_unpaid(self, reason):
        """If a "ship" or "order_pick" order is no longer fully paid — a
        partial or full refund settling, an invoice being cancelled, reset
        to draft, or any other way its payment backing disappears — drop
        it to reflect that, but NEVER automatically further than
        Order/Pick, regardless of how much (or how little) payment is
        left:

        - Still has SOME active payment (a partial refund): order_pick,
          clearance_date untouched — it keeps its original place in line
          rather than losing priority just because payment status changed.
        - Nothing is paid at all any more (a full refund, a cancelled or
          draft-reset invoice): STILL order_pick, clearance_date STILL
          untouched, its reservation STILL held. Automatically dropping a
          losing-payment order all the way to no_invoice used to release
          its stock immediately — that's no longer automatic at all. Going
          lower than Order/Pick is now only ever a deliberate,
          permission-gated manual override (see write() above) — a genuine
          cancellation is an explicit human decision, never an automatic
          side effect of an invoice merely disappearing or being corrected.

        `reason` is a short label for what actually triggered this check
        (e.g. "Invoice cancelled", "Refund settled") — logged on the order
        so its history shows why, not just that. Recorded on
        clearance_last_demotion_reason too, both so it's a filterable
        field rather than only chatter text, and so a repeat call in the
        same transaction (Odoo can recompute payment_state, which drives
        this, more than once per transaction) doesn't re-post the same
        note every time nothing has actually changed since.

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
            if order.fulfillment_stage == "ship":
                order.with_context(clearance_internal_write=True).write({
                    "fulfillment_stage": "order_pick",
                    "clearance_last_demotion_reason": reason,
                })
                order.message_post(body=(
                    f"{reason} — demoted from Ship to Order/Pick. Clearance "
                    f"timestamp untouched, keeps its original place in line. "
                    f"Only a manual override can drop it to No Invoice."
                ))
            elif (
                not order._has_active_payment()
                and order.clearance_last_demotion_reason != reason
            ):
                order.with_context(clearance_internal_write=True).write({
                    "clearance_last_demotion_reason": reason,
                })
                order.message_post(body=(
                    f"{reason} — no active payment remains. Stays at "
                    f"Order/Pick per policy: clearance timestamp and "
                    f"reservation untouched. Only a manual override can "
                    f"drop it to No Invoice."
                ))

    def _resolve_clearance_date(self, fresh_timestamp):
        """The timestamp to actually stamp when an order becomes eligible
        again — its backed-up ORIGINAL clearance_date if a manual override
        back to No Invoice previously wiped it, otherwise the fresh
        timestamp for a genuinely new clearance."""
        self.ensure_one()
        return self.clearance_date_backup or fresh_timestamp

    @api.model
    def _cron_reserve_by_clearance(self):
        self._reserve_by_clearance()

    @api.model
    def _clearance_priority_key_for_line(self, sale_line, simulate_now=False):
        """Ranks a sale.order.line by its own/its order's actual claim —
        never the order-wide queue_priority_bucket. Grouped and processed
        per product: fairness is a per-product concept, an order's turn
        for product A must never be decided by, or affect, its standing
        for product B.

        Split out of _clearance_move_priority (which needed a real move)
        so a line's priority can be asked about even with no move at all
        yet — a draft/sent order's line, simulated as "if this were
        confirmed right now" by _simulate_clearance_availability below,
        via simulate_now=True.

        Hard lock is deliberately absent from this ranking — its only job
        is protecting whatever an order ALREADY holds (guaranteed
        unconditionally elsewhere), not winning it any claim on stock that
        isn't already spoken for. A hard-locked order with nothing paid or
        force-reserved of its own competes for new stock at the very back,
        same as any other order with no real claim.
        """
        order = sale_line.order_id
        # A line that gave up its stock under "Scheduled Future Stock" (or
        # would still qualify to) outranks EVERYTHING else, force-reserve
        # included, for whatever arrives next on this product — force-
        # reserve only ever takes stock that's genuinely available; it must
        # never be able to grab the very future incoming shipment a holder
        # gave up its current stock specifically to reclaim. Ranked by its
        # own scheduled_date, earliest first, among each other.
        if sale_line.clearance_defer_reason == "Scheduled Future Stock":
            return (0, order.pick_scheduled_date or datetime.min)
        if sale_line.is_force_reserved:
            return (1, sale_line.force_reserved_date or datetime.min)
        if order.clearance_date:
            return (2, order.clearance_date)
        # A draft/sent order has no real clearance_date yet — simulate_now
        # treats it as "if this were confirmed right this instant", the
        # exact tier a genuine grace-period confirmation would land in,
        # rather than falling all the way to tier 3 (no real claim at
        # all), which would understate what a fresh confirmation would
        # actually be entitled to.
        if simulate_now:
            return (2, fields.Datetime.now())
        return (3, datetime.min)

    @api.model
    def _clearance_move_priority(self, move):
        """Thin move-based wrapper around _clearance_priority_key_for_line
        — hoisted out of _reserve_by_clearance as a real method (rather
        than a throwaway closure) so _forecast_incoming_allocation below
        can reuse the identical tiering for the not-yet-arrived side of
        the queue, instead of re-deriving it and risking the two drifting
        apart."""
        return self._clearance_priority_key_for_line(move.sale_line_id)

    def _clearance_is_far_future_move(self, move, far_future_cutoff):
        """Mirrors _reserve_by_clearance's own far-future exclusion exactly
        — hoisted so _forecast_incoming_allocation excludes the same demand
        from competing for FUTURE incoming that this method excludes from
        competing for CURRENT stock, for the same reason: an order months
        out has no more business getting first claim on an arriving PO than
        it has on today's on-hand stock. The one exception, both places: an
        order/line override (hard lock or force-reserve) always sticks
        regardless of how far out the order is scheduled.
        """
        sale_line = move.sale_line_id
        order = sale_line.order_id
        return bool(
            order.pick_scheduled_date
            and order.pick_scheduled_date > far_future_cutoff
            and not order.is_reservation_hard_locked
            and not sale_line.is_reservation_hard_locked
            and not sale_line.is_force_reserved
        )

    @api.model
    def _clearance_is_output_move(self, move):
        """Once a Ship-leg move has actually claimed real stock out of the
        warehouse's Output location, it's earmarked to this order — same
        automatic release-immunity category as a hard lock, force-reserve,
        or a Scheduled Future Stock holder, not a priority tier: it grants
        no acquisition power of its own, it only keeps what's already been
        won. No lock needed, and none can be toggled off — the moment a
        move genuinely sources from Output, this applies on every
        subsequent run for as long as it still does.

        A Pick-leg move's source is Stock (or a picking-zone sub-location
        of it), never Output, so this can never mistakenly protect a
        Pick-type move — only a Ship-leg move drawing from Output matches.
        """
        if move.state not in ("assigned", "partially_available"):
            return False
        warehouse = move.picking_id.picking_type_id.warehouse_id or move.picking_type_id.warehouse_id
        output_location = warehouse.wh_output_stock_loc_id
        if not output_location or not move.location_id.parent_path:
            return False
        return bool(
            move.location_id == output_location
            or move.location_id.parent_path.startswith(output_location.parent_path)
        )

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
            lambda m: self._clearance_is_far_future_move(m, far_future_cutoff)
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
        #
        # One case this must NOT skip: a Ship-leg move that just reached
        # Output via Odoo's own native move-chaining (completing its
        # origin Pick) arrives here already "assigned" — genuinely, with
        # nothing for this method to reassign — but still needs its
        # is_locked_reservation flag actually set, which only happens in
        # the loop below. Unlike hard-lock/force-reserve (flagged the
        # moment their own toggle fires, well before any fast path could
        # ever race them), nothing else ever flags an Output-arrived move,
        # so the fast path must fall through for it at least once.
        needs_output_flag = any(
            self._clearance_is_output_move(m) and not m.is_locked_reservation
            for m in all_moves
        )
        if (
            all_moves and all(m.state == "assigned" for m in all_moves)
            and not foreign_moves
            and not far_future_moves.filtered(lambda m: m.state == "assigned")
            and not needs_output_flag
        ):
            return {"order_count": len(orders), "reserved_move_count": 0}

        touched = all_moves | foreign_moves
        before_state = {m.id: (m.state, m.quantity) for m in touched}

        # Snapshot of genuinely free (unreserved, on-hand) quantity per
        # product, taken BEFORE this run's own blanket release below —
        # needed later so the "Scheduled Future Stock" targeted-release
        # pass can tell a holder that reclaimed truly NEW stock (a
        # receipt, a cancellation, anything that freed real stock
        # independent of this run) apart from a holder that's merely
        # cycling through the SAME stock this run's own blanket release
        # is about to free up moments from now. See the targeted-release
        # block below for exactly why that distinction matters — without
        # it, a holder that legitimately reclaims its promised shipment
        # can have that reclaim immediately clawed back and handed to an
        # ordinary, lower-tier competitor with no claim on it at all.
        holder_products = touched.filtered(
            lambda m: m.sale_line_id.clearance_defer_reason == "Scheduled Future Stock"
        ).product_id
        pre_release_free_qty = {}
        for product in holder_products:
            warehouse = touched.filtered(
                lambda m: m.product_id == product
            )[:1].sale_line_id.order_id.warehouse_id
            pre_release_free_qty[product.id] = product.with_context(
                location=warehouse.lot_stock_id.id
            ).free_qty

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
        # A move that's already reached Output is protected too — see
        # _clearance_is_output_move — automatically, with no lock needed.
        protected = touched.filtered(
            lambda m: m.is_locked_reservation
            or m.sale_line_id.order_id.is_reservation_hard_locked
            or m.sale_line_id.is_reservation_hard_locked
            or m.sale_line_id.clearance_defer_reason == "Scheduled Future Stock"
            or self._clearance_is_output_move(m)
        )
        releasable = (touched - protected).filtered(
            lambda m: m.state in ("assigned", "partially_available")
        )
        releasable._do_unreserve()

        all_moves = all_moves - far_future_moves

        # See _clearance_move_priority's own docstring for the tiering and
        # rationale — hoisted there so _forecast_incoming_allocation can
        # reuse the identical ranking.
        move_priority = self._clearance_move_priority

        by_product = {}
        for m in all_moves:
            by_product.setdefault(m.product_id.id, self.env["stock.move"])
            by_product[m.product_id.id] |= m

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
                # Same for a Ship-leg move that just reached Output on this
                # very pass — flagged immediately, not just protected from
                # release on some later run, so it's also immune to
                # cancel/relocate the instant it's genuinely at Output.
                order = move.sale_line_id.order_id
                sale_line = move.sale_line_id
                if (
                    move.state in ("assigned", "partially_available")
                    and not move.is_locked_reservation
                    and (
                        order.is_reservation_hard_locked
                        or sale_line.is_reservation_hard_locked
                        or sale_line.is_force_reserved
                        or self._clearance_is_output_move(move)
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
            # A holder that's already reached Output is exempt from even
            # this targeted trade — Output protection is unconditional,
            # same as a hard lock, not just immunity from the ordinary
            # blanket release above.
            holders = product_moves.filtered(
                lambda m: m.sale_line_id.clearance_defer_reason == "Scheduled Future Stock"
                and not self._clearance_is_output_move(m)
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
                #
                # Bug found live: a holder that has ALREADY released once
                # can be sitting here fully "assigned" for a completely
                # different reason than "still holding old stock it
                # could give up" — it may have just reclaimed its OWN
                # promised shipment moments ago, in this very same run's
                # main pass. is_scheduled_future_stock_release alone can't
                # tell those apart (it stays True across both), and
                # naively excluding on that flag alone breaks the
                # holder's legitimate FIRST release too, whenever that
                # release happens to be re-derived over two consecutive
                # runs (e.g. an earlier competitor's own action_confirm()
                # already triggered it once, then this method runs again
                # explicitly) — in that case nothing new actually arrived,
                # and giving the stock to the earlier-scheduled competitor
                # is exactly correct.
                #
                # The real distinguishing signal is pre_release_free_qty,
                # above: genuinely-free stock that existed BEFORE this
                # run's own blanket release even ran. If that was zero,
                # whatever this holder is now "assigned" MUST be recycled
                # from this run's own churn (nothing else could have
                # supplied it) — safe to take back, matching every
                # existing single-competitor scenario. If it was
                # positive, something real became available independent
                # of this run's churn — a genuine reclaim, which must be
                # protected rather than clawed back to a lower tier.
                eligible_holders = holders.filtered(
                    lambda m: m.state in ("assigned", "partially_available")
                    and not (
                        m.sale_line_id.is_scheduled_future_stock_release
                        and pre_release_free_qty.get(m.product_id.id, 0) > 0
                    )
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

    @api.model
    def _forecast_incoming_allocation(self, product_ids=None):
        """The forecast-only twin of _reserve_by_clearance, for stock that
        hasn't arrived yet: a live, never-persisted allocation of every
        committed future incoming PO shipment against every currently-open
        outgoing demand move, ranked in the EXACT same clearance-priority
        order real reservation uses (_clearance_move_priority) — so the
        forecast can never promise an outcome the real engine wouldn't
        actually produce once a shipment lands.

        Never calls _action_assign()/_do_unreserve() and never writes
        anything: pure computation over currently-committed state,
        re-derived from scratch on every call, same as bin_stock_total.

        Returns {move_id: [{"qty", "expected_date", "incoming_move_id",
        "purchase_order_id", "purchase_order_name",
        "purchase_order_confirmed"}, ...]} for every open
        demand move considered. An empty list means even ALL committed
        incoming for that product/warehouse won't help this move right
        now; a non-empty list may still sum to less than the move's own
        remaining demand (partial coverage) — see sale_order_line.py's
        expected_incoming_fully_covers_remaining.
        """
        move_domain = [
            ("state", "not in", ("done", "cancel", "assigned")),
            ("sale_line_id", "!=", False),
            "|",
            ("sale_line_id.order_id.fulfillment_stage", "in", ("order_pick", "ship", "grace_period")),
            ("sale_line_id.is_force_reserved", "=", True),
        ]
        if product_ids:
            move_domain.append(("product_id", "in", product_ids))
        demand_moves = self.env["stock.move"].search(move_domain)
        if not demand_moves:
            return {}

        # Only the leg that actually draws from warehouse stock competes
        # for a future incoming shipment landing in that same stock. A
        # multi-step route's downstream leg (Ship, Pack) is chained
        # ('waiting') behind the Pick leg for the SAME underlying customer
        # demand — counting it too would double-book the same units
        # against the incoming pool. _reserve_by_clearance never hits this
        # because _action_assign() on a still-waiting Ship move is a
        # harmless no-op; this method does real bucket arithmetic, so it
        # must filter explicitly.
        demand_moves = demand_moves.filtered(
            lambda m: m.picking_type_id == m.sale_line_id.order_id.warehouse_id.pick_type_id
        )
        if not demand_moves:
            return {}

        far_future_cutoff = fields.Datetime.now() + relativedelta(months=FAR_FUTURE_MONTHS)
        demand_moves = demand_moves.filtered(
            lambda m: not self._clearance_is_far_future_move(m, far_future_cutoff)
        )

        by_product_wh = {}
        for m in demand_moves:
            wh = m.sale_line_id.order_id.warehouse_id
            key = (m.product_id.id, wh.id)
            by_product_wh.setdefault(key, self.env["stock.move"])
            by_product_wh[key] |= m

        result = {}
        for (product_id, wh_id), moves in by_product_wh.items():
            product = self.env["product.product"].browse(product_id)
            warehouse = self.env["stock.warehouse"].browse(wh_id)
            incoming = self.env["sale.order.line"]._get_committed_future_incoming_moves_for_product(
                product, warehouse
            )
            if not incoming:
                for m in moves:
                    result[m.id] = []
                continue

            # Already date-ascending (see _get_committed_future_incoming_moves_for_product).
            buckets = [{"move": im, "remaining": im.product_qty} for im in incoming]

            for move in moves.sorted(key=self._clearance_move_priority):
                need = move.product_qty - move.quantity
                allocations = []
                for bucket in buckets:
                    if need <= 0:
                        break
                    take = min(need, bucket["remaining"])
                    if take <= 0:
                        continue
                    po = bucket["move"].purchase_line_id.order_id
                    allocations.append({
                        "qty": take,
                        "expected_date": bucket["move"].date,
                        "incoming_move_id": bucket["move"].id,
                        "purchase_order_id": po.id,
                        "purchase_order_name": po.name,
                        # True once the PO's container reference AND port
                        # arrival date are both on file (see
                        # purchase_order.py) — the receive date is then
                        # treated as confirmed, not just planned.
                        "purchase_order_confirmed": po.is_receipt_confirmed,
                    })
                    bucket["remaining"] -= take
                    need -= take
                result[move.id] = allocations
        return result

    @api.model
    def _simulate_clearance_availability(self, lines):
        """The draft-order twin of _forecast_incoming_allocation /
        _reserve_by_clearance, for a sale.order.line that has no
        stock.move at all yet (order still draft/sent — nothing to
        assign, nothing to search for by sale_line_id). Answers: if this
        order were confirmed right now, how much of this line's own
        demand would it actually get, and by when — inserted into the
        REAL queue at its real priority (as-if-confirmed-now for a line
        with no clearance_date yet, via
        _clearance_priority_key_for_line's simulate_now=True), ahead of
        or behind every genuinely competing REAL open demand for the
        same product/warehouse. Never assigns/writes anything: pure
        computation, re-derived from scratch on every call.

        Two supply phases, walked together in priority order:

          1. On-hand free_qty at the warehouse's own Stock location —
             already nets out every CURRENT reservation, so no need to
             re-simulate existing holders individually.
          2. Future committed incoming (same buckets
             _forecast_incoming_allocation itself builds).

        Every REAL claim ranked ahead of a given hypothetical line takes
        its own share of on-hand first, then future buckets in date
        order, before the hypothetical gets anything — the exact
        allocation order the real engine would use once confirmed.

        Returns {line_id: {"qty_now", "chunks": [{"qty",
        "expected_date", "purchase_order_id", "purchase_order_name",
        "purchase_order_confirmed"}], "total_qty"}}.
        """
        lines = lines.filtered(lambda l: l.product_id and l.product_uom_qty and l.order_id.warehouse_id)
        if not lines:
            return {}

        by_product_wh = {}
        for line in lines:
            key = (line.product_id.id, line.order_id.warehouse_id.id)
            by_product_wh.setdefault(key, self.env["sale.order.line"])
            by_product_wh[key] |= line

        far_future_cutoff = fields.Datetime.now() + relativedelta(months=FAR_FUTURE_MONTHS)
        # Same domain _forecast_incoming_allocation uses for genuinely
        # open (not yet holding anything) real demand — this simulation
        # only needs to know what's AHEAD of the hypothetical in the
        # queue; anything already assigned has already taken its share
        # out of free_qty, which on-hand below already reflects.
        move_domain_base = [
            ("state", "not in", ("done", "cancel", "assigned")),
            ("sale_line_id", "!=", False),
            "|",
            ("sale_line_id.order_id.fulfillment_stage", "in", ("order_pick", "ship", "grace_period")),
            ("sale_line_id.is_force_reserved", "=", True),
        ]

        result = {}
        for (product_id, wh_id), hypo_lines in by_product_wh.items():
            product = self.env["product.product"].browse(product_id)
            warehouse = self.env["stock.warehouse"].browse(wh_id)

            real_moves = self.env["stock.move"].search(
                move_domain_base + [("product_id", "=", product_id)]
            ).filtered(
                lambda m: m.sale_line_id.order_id.warehouse_id == warehouse
                and m.picking_type_id == warehouse.pick_type_id
                and not self._clearance_is_far_future_move(m, far_future_cutoff)
            )

            on_hand_remaining = product.with_context(location=warehouse.lot_stock_id.id).free_qty
            incoming = self.env["sale.order.line"]._get_committed_future_incoming_moves_for_product(
                product, warehouse
            )
            buckets = [{"move": im, "remaining": im.product_qty} for im in incoming]

            claims = [
                {"key": self._clearance_move_priority(m), "need": m.product_qty - m.quantity, "hypo_line_id": None}
                for m in real_moves
            ] + [
                {
                    "key": self._clearance_priority_key_for_line(line, simulate_now=True),
                    "need": line.product_uom_qty,
                    "hypo_line_id": line.id,
                }
                for line in hypo_lines
            ]
            claims.sort(key=lambda c: c["key"])

            for claim in claims:
                need = claim["need"]
                qty_now = min(need, on_hand_remaining)
                on_hand_remaining -= qty_now
                need -= qty_now
                chunks = []
                for bucket in buckets:
                    if need <= 0:
                        break
                    take = min(need, bucket["remaining"])
                    if take <= 0:
                        continue
                    po = bucket["move"].purchase_line_id.order_id
                    chunks.append({
                        "qty": take,
                        "expected_date": bucket["move"].date,
                        "purchase_order_id": po.id,
                        "purchase_order_name": po.name,
                        "purchase_order_confirmed": po.is_receipt_confirmed,
                    })
                    bucket["remaining"] -= take
                    need -= take
                if claim["hypo_line_id"] is not None:
                    result[claim["hypo_line_id"]] = {
                        "qty_now": qty_now,
                        "chunks": chunks,
                        "total_qty": qty_now + sum(c["qty"] for c in chunks),
                    }
        return result
