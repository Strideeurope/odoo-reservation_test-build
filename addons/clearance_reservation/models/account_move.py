from odoo import models, fields


class AccountMove(models.Model):
    _inherit = "account.move"

    def _get_clearance_timestamp(self):
        """The real moment this invoice's receivable was reconciled against a
        payment, not the moment this hook happens to run. account.payment
        only carries a Date (day-granularity) field, which can't tell two
        same-day payments apart — account.partial.reconcile.create_date is a
        true Datetime stamped when the reconciliation was actually recorded,
        which is what makes the clearance queue race-proof.
        """
        self.ensure_one()
        receivable_lines = self.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable"
        )
        reconciles = receivable_lines.matched_debit_ids | receivable_lines.matched_credit_ids
        dates = reconciles.mapped("create_date")
        if dates:
            return min(dates)
        # Fallback only — no reconciliation found at all.
        return fields.Datetime.now()

    def _compute_payment_state(self):
        # payment_state is a stored compute field driven by reconciliation —
        # it is recomputed through the ORM's internal flush, which does NOT
        # go through write(). Hooking write() for "payment_state" in vals
        # (the obvious approach) silently never fires. This compute override
        # is the only reliable place to react to it changing.
        #
        # Deliberately NOT diffing old-vs-new payment_state here: Odoo's
        # compute engine can invoke this multiple times per transaction as
        # dependencies resolve, and an old-value snapshot taken at the start
        # of one of those calls can already reflect a value set by an
        # earlier call in the same flush — silently swallowing the one call
        # that actually saw the transition. Applying the logic unconditionally
        # is safe because _clearance_apply_payment_state is idempotent: it
        # only acts on the absence of clearance_date, and _try_promote_to_ship
        # re-checks current state rather than a transition.
        super()._compute_payment_state()
        for move in self:
            if move.move_type == "out_invoice":
                move._clearance_apply_payment_state()
            elif move.move_type == "out_refund":
                move._clearance_apply_refund_payment_state()

    def _clearance_apply_payment_state(self):
        self.ensure_one()
        orders = self.invoice_line_ids.sale_line_ids.order_id
        if not orders:
            return

        payment_active = self.payment_state in ("partial", "in_payment", "paid")

        # Real payment always takes over the timestamp — whether the
        # order never had one at all, or its current one came from a
        # manual override rather than genuine payment. An override lets
        # an order act as if cleared in the meantime (see sale_order.py's
        # write()), but the moment genuine payment actually arrives, THAT
        # becomes its real, permanent place in line — an override is a
        # stand-in, never something that outlives the real thing showing
        # up.
        needs_real_timestamp = orders.filtered(
            lambda o: payment_active and (not o.clearance_date or o.clearance_is_override)
        )
        if needs_real_timestamp:
            real_timestamp = self._get_clearance_timestamp()
            # One at a time — an order's own backup (if any) only ever
            # applies to ITS history, a shared batched value would be
            # wrong the moment any two orders in this set differ on that.
            for order in needs_real_timestamp:
                was_override = order.clearance_is_override
                if order.clearance_date and order.clearance_is_override:
                    # A fabricated override timestamp is never worth
                    # preserving — real payment always gets a genuinely
                    # fresh timestamp here, discarding it outright (any
                    # backup would already be empty in this state: the
                    # override itself consumed it the moment it stamped
                    # this fabricated date in the first place).
                    new_date = real_timestamp
                    restored_backup = False
                else:
                    # No clearance_date at all right now — restore a
                    # backed-up ORIGINAL timestamp if this order has one
                    # (a prior full refund), so it doesn't lose its true
                    # historical place in line just because it's being
                    # genuinely cleared again; otherwise this really is
                    # brand new, so the fresh timestamp is correct.
                    restored_backup = bool(order.clearance_date_backup)
                    new_date = order._resolve_clearance_date(real_timestamp)
                order.with_context(clearance_internal_write=True).write({
                    "clearance_date": new_date,
                    "clearance_date_backup": False,
                    "clearance_is_override": False,
                    "clearance_last_demotion_reason": False,
                })
                if was_override:
                    order.message_post(body=(
                        f"Payment received — replaced the provisional override "
                        f"timestamp with the real one ({new_date}); override flag "
                        f"cleared."
                    ))
                elif restored_backup:
                    order.message_post(body=(
                        f"Payment received — restored its original clearance "
                        f"timestamp ({new_date}) from before it lost payment, "
                        f"rather than a fresh stamp."
                    ))
                else:
                    order.message_post(body=f"Payment received — clearance timestamp set to {new_date}.")

        # Advance the STAGE too, but only for orders that hadn't already
        # been elevated some other way (an override, an earlier payment
        # cycle): a no_invoice order enters the queue at order_pick for
        # the first time; a grace_period order graduates to order_pick,
        # keeping whatever clearance_date it now has (either its
        # original grace-period one, or the just-replaced real one from
        # above if it had been override-cleared). An order already
        # sitting at order_pick/ship (via override) stays exactly where
        # it is — real payment legitimizes its timestamp, it doesn't
        # downgrade a stage that's already ahead.
        entering_queue = orders.filtered(
            lambda o: o.fulfillment_stage in ("no_invoice", "grace_period") and payment_active
        )
        if entering_queue:
            entering_queue.with_context(clearance_internal_write=True).write({
                "fulfillment_stage": "order_pick",
            })
            for order in entering_queue:
                order.message_post(body="Payment received — advanced to Order/Pick, now competing for stock on genuine clearance priority.")

        if self.payment_state == "paid":
            orders._try_promote_to_ship()
        else:
            # Payment reversed (unreconciled, credit note, etc.) after
            # already reaching Ship — ship depends on payment status alone,
            # so losing "paid" status demotes it back just as directly as
            # gaining it promoted it.
            orders._demote_from_ship_if_unpaid(reason="Payment reversed on the invoice")

    def _clearance_apply_refund_payment_state(self):
        """A refund (credit note) reversing an order's payment is invisible
        to _clearance_apply_payment_state above — that method only ever
        looks at out_invoice moves, and _is_fully_paid() itself checks the
        refund separately (see sale_order.py). This only needs to trigger
        the demote check: a refund can never be what CLEARS an order in
        the first place, only what un-clears one that already was."""
        self.ensure_one()
        orders = self.invoice_line_ids.sale_line_ids.order_id
        if orders:
            orders._demote_from_ship_if_unpaid(reason="Refund settled")

    def write(self, vals):
        # A posted invoice being cancelled or reset to draft loses its
        # standing exactly like a refund does — but neither reconciliation
        # change nor payment_state necessarily moves as a result (an
        # invoice that was never reconciled in the first place has nothing
        # to unreconcile), so this can't be left to _compute_payment_state
        # above to catch. Snapshotting the affected orders BEFORE
        # super().write() runs: once state is no longer "posted", nothing
        # downstream should still treat this as a live invoice.
        demote_reason = None
        orders = self.env["sale.order"]
        if "state" in vals and vals["state"] in ("cancel", "draft"):
            leaving_posted = self.filtered(
                lambda m: m.move_type == "out_invoice" and m.state == "posted"
            )
            if leaving_posted:
                orders = leaving_posted.invoice_line_ids.sale_line_ids.order_id
                demote_reason = (
                    "Invoice cancelled" if vals["state"] == "cancel"
                    else "Invoice reset to draft"
                )
        res = super().write(vals)
        if orders:
            orders._demote_from_ship_if_unpaid(reason=demote_reason)
        return res
