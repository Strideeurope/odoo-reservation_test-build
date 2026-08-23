from datetime import timedelta
from unittest.mock import patch

from dateutil.relativedelta import relativedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.clearance_reservation.models.sale_order import GRACE_PERIOD_DAYS


@tagged("post_install", "-at_install")
class TestClearanceReservation(TransactionCase):
    """Regression tests for bugs found and fixed during interactive testing,
    and for the stock-priority ranking (force-reserve > genuine payment,
    with hard lock granting NO acquisition ability at all — see below)
    established by explicit product decision:
    - action_force_reserve() failing to flag an already-assigned move
    - force-reserved lines being invisible to _reserve_by_clearance's domain
    - releasing a forced reservation leaving its move permanently soft-locked
    - the priority ranking for NEWLY available stock: force-reserve then
      genuine payment — and that stock already held by ANY tier, hard lock
      included, is never reclaimable by anyone. Hard lock is deliberately
      absent from both this ranking AND the queue's eligibility domain: its
      ONLY function is protecting whatever an order already holds (however
      it got it — a native manual reservation, or payment gained later)
      from ever being unreserved. A no_invoice order that is only
      hard-locked, with no genuine clearance and no force-reserve, is never
      attempted by the queue and can never acquire so much as one new unit
      through it, uncontested or not
    - the order-level hard-lock release cleanup missing the Ship leg
    - the no-clearance-timestamp picking validation gate
    - Ship depending on Pick-step fulfillment as well as payment (reversed
      by explicit product decision: Ship now depends on payment alone, so
      an incomplete order can still ship whatever it does have)
    - activating a hard lock or force-reserve unreserving stock a genuinely
      paid/queued order already legitimately held, via the old
      release-everything-then-reallocate-from-scratch approach — an
      override must only ever compete for currently-free stock, never
      claw back an existing legitimate reservation at any tier
    - the pick_fully_validated freeze condition
    - orders scheduled more than 6 months out excluded from competing for
      today's stock, unless hard-locked or force-reserved
    - the manual Override Stage field failing to stick at Ship on an
      unpaid order, because _reserve_by_clearance's own generic re-runs
      used to immediately demote it back — promote/demote now lives
      exclusively in the payment-change hook (account_move.py)
    - "Scheduled Future Stock": a line already holding stock may give it
      up to a strictly earlier-scheduled competitor for the same product
      if a COMMITTED (never draft/RFQ) future incoming shipment is
      verified to cover its own demand with a real time and quantity
      buffer. Three safety-guarantee bugs were found and fixed building
      this:
      - the tag (and the elevated reclaim priority it grants in
        move_priority) was computed purely from currently-held move
        state, so it vanished the instant a holder actually gave up its
        stock — exactly when the guarantee mattered most. Fixed with a
        persistent is_scheduled_future_stock_release flag that survives
        holding nothing at all, until the line reclaims enough stock or
        its safety net evaporates.
      - force-reserve originally outranked this reclaim tier in
        move_priority, meaning a force-reserve could grab a holder's own
        promised future shipment the moment it arrived — reversed by
        explicit product decision: force-reserve only ever takes
        genuinely available stock, never what a released holder is owed.
      - the targeted release pass itself judged eligibility purely by
        scheduled date, so a force-reserved order with an earlier
        scheduled date could still trigger a release from a holder even
        after the tier fix above — closed by excluding force-reserved
        lines from that pass's demand pool entirely.
      - found via live data: the "Scheduled Future Stock" tag itself was
        computed with no check on whether the order was even eligible to
        be in the queue at all — a no_invoice order (no payment, no
        override, no lock) that happened to still hold stock and
        coincidentally match a future incoming shipment kept the tag
        (and the reclaim protection it grants) indefinitely, letting it
        squat on stock it had zero legitimate claim to.
    - explicit product decision: reservation must succeed as long as
      stock exists anywhere in the warehouse (Picking Zone or Buffer
      Zone), not just Picking Zone alone — via an instant, already-
      validated internal transfer (never a pending one left for a human
      to validate first). The Pick move itself always stays sourced from
      Picking Zone only. Separately, Odoo's native "Relocate" quant
      action can move already-reserved stock with no error, but silently
      drops the reservation as a side effect — the module re-runs the
      queue right after any such relocation so the affected order
      automatically reclaims its spot.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.warehouse = cls.env["stock.warehouse"].search([], limit=1)
        # Resolved dynamically, not hard-coded to lot_stock_id — the
        # warehouse's actual Pick source location is whatever its
        # "Stock -> Customers" rule currently points at (may be scoped to
        # a specific sub-location, e.g. a dedicated picking zone, rather
        # than the flat top-level Stock location).
        pick_rule = cls.env["stock.rule"].search(
            [("picking_type_id", "=", cls.warehouse.pick_type_id.id)], limit=1
        )
        cls.pick_source_location = pick_rule.location_src_id or cls.warehouse.lot_stock_id
        # A fresh location, wired to the warehouse's own
        # buffer_zone_location_id field — deliberately not assuming any
        # pre-existing Buffer Zone location, so these tests are
        # self-contained regardless of what a real deployment has
        # configured.
        cls.buffer_location = cls.env["stock.location"].create({
            "name": "Test Buffer Zone",
            "location_id": cls.warehouse.view_location_id.id,
            "usage": "internal",
        })
        cls.warehouse.buffer_zone_location_id = cls.buffer_location.id
        cls.product = cls.env["product.product"].create({
            "name": "Test Clearance Widget",
            "is_storable": True,
        })
        cls.partner_a = cls.env["res.partner"].create({"name": "Test Partner A"})
        cls.partner_b = cls.env["res.partner"].create({"name": "Test Partner B"})
        cls.partner_c = cls.env["res.partner"].create({"name": "Test Partner C"})

    def _set_stock(self, qty, product=None):
        product = product or self.product
        quant = self.env["stock.quant"].search([
            ("product_id", "=", product.id),
            ("location_id", "=", self.pick_source_location.id),
        ])
        if not quant:
            quant = self.env["stock.quant"].create({
                "product_id": product.id,
                "location_id": self.pick_source_location.id,
            })
        quant.with_context(inventory_mode=True).write({"inventory_quantity": qty})
        quant.action_apply_inventory()

    def _set_buffer_stock(self, qty, product=None):
        product = product or self.product
        quant = self.env["stock.quant"].search([
            ("product_id", "=", product.id),
            ("location_id", "=", self.buffer_location.id),
        ])
        if not quant:
            quant = self.env["stock.quant"].create({
                "product_id": product.id,
                "location_id": self.buffer_location.id,
            })
        quant.with_context(inventory_mode=True).write({"inventory_quantity": qty})
        quant.action_apply_inventory()

    def _make_order(self, partner, qty, product=None):
        order = self.env["sale.order"].create({
            "partner_id": partner.id,
            "order_line": [(0, 0, {
                "product_id": (product or self.product).id,
                "product_uom_qty": qty,
            })],
        })
        order.action_confirm()
        return order

    def _make_admin_only_order(self, partner, qty, product=None):
        """A confirmed order forced back to a bare no_invoice/no-clearance
        state — the precondition every hard-lock/force-reserve-only test
        needs, since action_confirm() always grants a real clearance_date
        via the grace_period window on its own."""
        order = self._make_order(partner, qty, product=product)
        order.with_context(clearance_internal_write=True).write({
            "fulfillment_stage": "no_invoice",
            "clearance_date": False,
        })
        return order

    def _create_committed_po(self, qty, date_planned, product=None):
        product = product or self.product
        po = self.env["purchase.order"].create({
            "partner_id": self.partner_b.id,
            "order_line": [(0, 0, {
                "product_id": product.id,
                "name": product.name,
                "product_qty": qty,
                "product_uom": product.uom_po_id.id,
                "price_unit": 1.0,
                "date_planned": date_planned,
            })],
        })
        po.button_confirm()
        return po

    def _pay_order(self, order):
        invoice = order._create_invoices()
        invoice.action_post()
        register = self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=invoice.ids
        ).create({"payment_date": fields.Date.today()})
        register._create_payments()
        return invoice

    def test_force_reserve_flags_already_assigned_move(self):
        """Bug: action_force_reserve() only looked at moves NOT yet
        assigned, so a line with abundant, uncontested stock — already
        auto-assigned by the time anyone clicks the button — silently
        reported is_force_reserved=False despite holding the stock."""
        self._set_stock(100)
        order = self._make_order(self.partner_a, 10)
        line = order.order_line
        move = line.move_ids
        self.assertEqual(move.state, "assigned", "abundant stock should auto-reserve on confirm")
        self.assertFalse(line.is_force_reserved)

        line.action_force_reserve()

        self.assertTrue(line.is_force_reserved)
        self.assertTrue(move.is_locked_reservation)

    def test_force_reserved_line_visible_to_reservation_domain(self):
        """Bug: a line that is ONLY force-reserved (no hard lock, no real
        clearance_date) was completely absent from _reserve_by_clearance's
        search domain — action_force_reserve() failing once (no stock)
        meant no later event (quant update, incoming receipt, cron) could
        ever retry it."""
        self._set_stock(0)
        order = self._make_admin_only_order(self.partner_a, 10)
        line = order.order_line
        line.action_force_reserve()
        self.assertFalse(line.is_force_reserved, "nothing was available to grab yet")
        self.assertEqual(line.move_ids.state, "confirmed")

        # Simulate the exact bug precondition: the line is flagged
        # force-reserved (e.g. from an earlier attempt that briefly
        # succeeded before losing the stock again) while its move itself
        # sits completely unreserved.
        line.write({"is_force_reserved": True})

        # _set_stock's own quant-update hook triggers _reserve_by_clearance
        # automatically — no explicit call needed, which is exactly the
        # behavior this test is proving: the fix means this line no longer
        # needs a fresh manual retry, an ordinary stock event is enough.
        self._set_stock(10)

        line.invalidate_recordset()
        self.assertEqual(line.move_ids.state, "assigned")
        self.assertTrue(line.is_force_reserved)
        self.assertTrue(line.move_ids.is_locked_reservation)

    def test_force_reserve_release_clears_move_lock(self):
        """Bug: turning off is_force_reserved cleared force_reserved_date
        but left is_locked_reservation set on the move, so a later attempt
        to release the reservation (e.g. via the native Unreserve action)
        raised "these reservations are locked and cannot be released"
        forever, even though the override had genuinely been turned off."""
        self._set_stock(10)
        order = self._make_admin_only_order(self.partner_a, 10)
        order.order_line.action_force_reserve()
        self.assertTrue(order.order_line.is_force_reserved)
        self.assertTrue(order.order_line.move_ids.is_locked_reservation)

        order.order_line.write({"is_force_reserved": False})

        self.assertFalse(
            order.order_line.move_ids.is_locked_reservation,
            "the move lock must be released along with the override itself",
        )
        # Would have raised "these reservations are locked..." before the fix.
        order.order_line.move_ids._do_unreserve()
        self.assertEqual(order.order_line.move_ids.state, "confirmed")

    def test_hard_lock_only_protects_existing_reservations(self):
        """Explicit product decision: hard lock has NO acquisition ability
        of its own at all. An order that is only hard-locked — no genuine
        clearance, no force-reserve — is never attempted by the queue and
        can never gain so much as one new unit through it, no matter how
        much stock arrives or how uncontested it is. Its sole function is
        protecting whatever the order already holds, however it got it,
        from ever being unreserved."""
        self._set_stock(10)
        order_locked = self._make_admin_only_order(self.partner_a, 10)
        order_locked.write({"is_reservation_hard_locked": True})
        self.env["sale.order"]._reserve_by_clearance(product_ids=[self.product.id])
        order_locked.order_line.invalidate_recordset()
        self.assertEqual(
            order_locked.order_line.move_ids.quantity, 0,
            "a hard lock alone, with no payment, must never acquire stock — even fully uncontested",
        )
        self.assertEqual(order_locked.queue_priority_bucket, 0, "still tier 0 for UI sorting purposes only")

        # Stock reaches it some OTHER way — a native manual reservation,
        # e.g. — simulated here with a direct _action_assign(). THIS is
        # what hard lock actually protects.
        order_locked.order_line.move_ids._action_assign()
        order_locked.order_line.invalidate_recordset()
        self.assertEqual(order_locked.order_line.move_ids.quantity, 10, "granted by something other than the queue")

        with self.assertRaises(UserError):
            order_locked.order_line.move_ids._do_unreserve()

        # A second, still-unfulfilled hard lock must not jump ahead of a
        # genuine force-reserve for stock that arrives next either.
        order_locked_2 = self._make_admin_only_order(self.partner_c, 5)
        order_locked_2.write({"is_reservation_hard_locked": True})
        order_force = self._make_admin_only_order(self.partner_b, 3)
        order_force.order_line.write({"is_force_reserved": True})

        self._set_stock(13)  # 3 new units — enough for the force-reserve, not the second lock too
        self.env["sale.order"]._reserve_by_clearance(product_ids=[self.product.id])
        order_locked.order_line.invalidate_recordset()
        order_locked_2.order_line.invalidate_recordset()
        order_force.order_line.invalidate_recordset()

        self.assertEqual(order_locked.order_line.move_ids.quantity, 10, "first hard lock's existing hold is untouched")
        self.assertEqual(order_force.order_line.move_ids.quantity, 3, "force-reserve claims the new stock")
        self.assertEqual(
            order_locked_2.order_line.move_ids.quantity, 0,
            "the second hard lock has no acquisition ability at all — it never competes, not even for leftovers",
        )

    def test_hard_lock_does_not_steal_already_held_clearance_stock(self):
        """Bug: activating a hard lock (or force-reserve) triggered a full
        release-and-reallocate pass that could unreserve stock a genuinely
        paid order already legitimately held, purely because the new
        override outranks payment for stock in general. An override must
        only ever grab stock that's currently free — if there isn't any,
        it waits at the front of the queue for the next arrival instead of
        clawing back an existing reservation at any tier."""
        self._set_stock(10)
        order_paid = self._make_order(self.partner_a, 10)
        self._pay_order(order_paid)
        order_paid.order_line.invalidate_recordset()
        self.assertEqual(order_paid.order_line.move_ids.quantity, 10, "paid order legitimately holds all 10")

        order_locked = self._make_admin_only_order(self.partner_b, 5)
        order_locked.write({"is_reservation_hard_locked": True})

        order_paid.order_line.invalidate_recordset()
        order_locked.order_line.invalidate_recordset()
        self.assertEqual(
            order_paid.order_line.move_ids.quantity, 10,
            "hard lock must not unreserve stock a paid order already legitimately holds",
        )
        self.assertEqual(
            order_locked.order_line.move_ids.quantity, 0,
            "hard lock has no acquisition ability at all — nothing was free anyway",
        )

        # 5 new units arrive: order_locked STILL gets none of it, even
        # though nothing else is competing for it — hard lock alone never
        # acquires anything, uncontested or not.
        self._set_stock(15)
        order_paid.order_line.invalidate_recordset()
        order_locked.order_line.invalidate_recordset()
        self.assertEqual(order_paid.order_line.move_ids.quantity, 10, "still untouched")
        self.assertEqual(
            order_locked.order_line.move_ids.quantity, 0,
            "hard lock never acquires new stock, even when nothing else wants it",
        )

    def test_force_reserve_outranks_payment_but_hard_lock_grants_none(self):
        """Explicit product decision: force-reserve is the one active
        override that jumps genuinely paid orders for stock that isn't
        already claimed — payment only gets what force-reserve didn't
        need. Hard lock is fundamentally different: it has no acquisition
        ability at all, only protection — whatever it already holds
        (granted some other way) stays untouchable forever, but it gains
        nothing further, not even stock nobody else wants.
        """
        self._set_stock(10)
        order_locked = self._make_admin_only_order(self.partner_a, 20)
        # Hard lock can't acquire this itself — granted here by a direct
        # assign to simulate a reservation from some other path, exactly
        # like test_hard_lock_only_protects_existing_reservations.
        order_locked.order_line.move_ids._action_assign()
        order_locked.write({"is_reservation_hard_locked": True})
        order_locked.order_line.invalidate_recordset()
        self.assertEqual(order_locked.order_line.move_ids.quantity, 10)

        order_paid = self._make_order(self.partner_c, 20)
        self._pay_order(order_paid)
        order_paid.order_line.invalidate_recordset()
        self.assertEqual(
            order_paid.order_line.move_ids.quantity, 0,
            "hard lock's existing hold is absolute — no tier, however ranked, can reclaim stock already held",
        )

        order_force = self._make_admin_only_order(self.partner_b, 5)
        order_force.order_line.write({"is_force_reserved": True})
        self.assertEqual(order_force.queue_priority_bucket, 1)

        # 25 new units arrive: force-reserve (the one tier that actively
        # outranks payment) is served first, then genuine payment gets the
        # rest — the still-unfulfilled hard lock, having no payment or
        # force-reserve of its own, gets none of it. It still only holds
        # the 10 it already had.
        self._set_stock(35)
        self.env["sale.order"]._reserve_by_clearance(product_ids=[self.product.id])
        order_locked.order_line.invalidate_recordset()
        order_force.order_line.invalidate_recordset()
        order_paid.order_line.invalidate_recordset()

        self.assertEqual(order_force.order_line.move_ids.quantity, 5, "force-reserve's full demand, served first")
        self.assertEqual(order_paid.order_line.move_ids.quantity, 20, "genuine payment gets the rest of the new stock")
        self.assertEqual(
            order_locked.order_line.move_ids.quantity, 10,
            "hard lock grants no claim on new stock — still only what it already held",
        )

    def test_ship_depends_only_on_payment_not_pick_fulfillment(self):
        """Explicit product decision, reversing the module's original
        design: Ship no longer requires the Pick step to be fully reserved
        — an order should be able to ship whatever it has as soon as it's
        paid, even if one of its lines never got any stock at all."""
        self._set_stock(5)
        product2 = self.env["product.product"].create({
            "name": "Test Clearance Widget 3", "is_storable": True,
        })
        # Deliberately no stock at all for product2.
        order = self.env["sale.order"].create({
            "partner_id": self.partner_a.id,
            "order_line": [
                (0, 0, {"product_id": self.product.id, "product_uom_qty": 5}),
                (0, 0, {"product_id": product2.id, "product_uom_qty": 5}),
            ],
        })
        order.action_confirm()
        line1 = order.order_line.filtered(lambda l: l.product_id == self.product)
        line2 = order.order_line.filtered(lambda l: l.product_id == product2)
        self.assertEqual(line1.move_ids.state, "assigned")
        self.assertEqual(line2.move_ids.state, "confirmed", "no stock at all for this product")

        self._pay_order(order)

        self.assertEqual(
            order.fulfillment_stage, "ship",
            "a fully-paid order must reach Ship even though one of its lines has no stock reserved at all",
        )

    def test_full_refund_demotes_to_no_invoice_and_backs_up_clearance(self):
        """Bug: _is_fully_paid() only ever looked at out_invoice moves, so
        refunding a fully-paid, shipped order left it sitting in Ship
        forever — the refund itself was invisible both to the paid-status
        check AND to the payment-change hook, which never fired for
        out_refund moves at all.

        Explicit product decision, on top of the fix: a FULL refund (no
        money left paid at all) must drop the order all the way to
        no_invoice, not just order_pick — it has no more genuine claim on
        stock than any other unpaid order. Its original clearance_date is
        backed up rather than discarded, so it isn't punished with a
        fresh, later timestamp if it gets paid again.
        """
        self._set_stock(10)
        order = self._make_order(self.partner_a, 10)
        invoice = self._pay_order(order)
        self.assertEqual(order.fulfillment_stage, "ship")
        original_clearance = order.clearance_date

        refund = invoice._reverse_moves(cancel=False)
        refund.action_post()
        register = self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=refund.ids
        ).create({"payment_date": fields.Date.today()})
        register._create_payments()

        self.assertFalse(order._is_fully_paid(), "a settled refund must reverse the paid status")
        self.assertFalse(order._has_active_payment(), "nothing is paid at all any more")
        self.assertEqual(
            order.fulfillment_stage, "no_invoice",
            "a full refund must drop the order all the way to no_invoice, not just order_pick",
        )
        self.assertFalse(order.clearance_date, "no genuine claim left while unpaid")
        self.assertEqual(
            order.clearance_date_backup, original_clearance,
            "the original clearance_date must be preserved, not discarded",
        )

        # Paying it again must restore the ORIGINAL timestamp, not stamp
        # a fresh one that would send it to the back of the line.
        self._pay_order(order)
        self.assertEqual(order.fulfillment_stage, "ship")
        self.assertEqual(
            order.clearance_date, original_clearance,
            "getting paid again must restore the order's original place in line",
        )
        self.assertFalse(order.clearance_date_backup, "the backup is consumed once restored")

    def test_full_refund_then_real_payment_restores_backup_precisely(self):
        """Regression: a later rework of the payment hook (to let a
        manual override's fabricated timestamp be replaced by real
        payment) accidentally dropped the backup restoration entirely —
        it unconditionally stamped a brand-new timestamp and discarded
        the backup, no matter what. Invisible in a fast test where
        "fresh" and "original" can coincidentally land on the exact same
        value (same ORM flush), which is exactly what let it slip past
        the original version of this test — pinned down here with a
        controlled, unmistakably distinct sentinel timestamp instead of
        relying on wall-clock timing."""
        self._set_stock(10)
        order = self._make_order(self.partner_a, 10)
        original_clearance = order.clearance_date
        invoice = self._pay_order(order)

        refund = invoice._reverse_moves(cancel=False)
        refund.action_post()
        register = self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=refund.ids
        ).create({"payment_date": fields.Date.today()})
        register._create_payments()
        self.assertEqual(order.fulfillment_stage, "no_invoice")
        self.assertEqual(order.clearance_date_backup, original_clearance)

        sentinel = fields.Datetime.now() + relativedelta(years=5)
        with patch(
            "odoo.addons.clearance_reservation.models.account_move.AccountMove._get_clearance_timestamp",
            return_value=sentinel,
        ):
            self._pay_order(order)
            # Read (forcing any deferred compute/flush) while the patch
            # is still active — see the identical note in
            # test_override_stamps_a_flagged_timestamp_that_real_payment_replaces.
            self.assertNotEqual(order.clearance_date, sentinel, "must NOT have used the fresh timestamp")
            self.assertEqual(
                order.clearance_date, original_clearance,
                "a backed-up original timestamp must be restored on real repayment, never replaced by a fresh one",
            )
            self.assertFalse(order.clearance_date_backup, "the backup is consumed once restored")

    def test_first_ever_payment_with_no_backup_gets_a_fresh_timestamp(self):
        """The other half of the same fix: an order with NO backup at all
        (never cleared before) must still get a genuinely fresh
        timestamp on its first real payment — _resolve_clearance_date
        falling through to the fresh value correctly, rather than always
        restoring (nothing to restore) or always discarding."""
        self._set_stock(10)
        order = self._make_admin_only_order(self.partner_a, 10)
        self.assertFalse(order.clearance_date_backup)

        sentinel = fields.Datetime.now() + relativedelta(years=5)
        with patch(
            "odoo.addons.clearance_reservation.models.account_move.AccountMove._get_clearance_timestamp",
            return_value=sentinel,
        ):
            self._pay_order(order)
            self.assertEqual(order.clearance_date, sentinel, "no backup exists — the fresh timestamp must be used")

    def test_override_after_full_refund_restores_backup_not_a_fresh_stamp(self):
        """An admin override bringing a fully-refunded order back into
        the queue must ALSO restore its backed-up original timestamp,
        not stamp a brand-new one — it's still flagged
        clearance_is_override (no genuine payment is currently active),
        but the value underneath is the order's true original place in
        line, exactly as if it had never lost it."""
        self._set_stock(10)
        order = self._make_order(self.partner_a, 10)
        original_clearance = order.clearance_date
        invoice = self._pay_order(order)

        refund = invoice._reverse_moves(cancel=False)
        refund.action_post()
        register = self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=refund.ids
        ).create({"payment_date": fields.Date.today()})
        register._create_payments()
        self.assertEqual(order.clearance_date_backup, original_clearance)

        order.write({"fulfillment_stage": "order_pick"})

        self.assertEqual(
            order.clearance_date, original_clearance,
            "an override restoring a backed-up order must use its original timestamp, not a fresh one",
        )
        self.assertTrue(order.clearance_is_override, "still flagged as override — no genuine payment is currently active")
        self.assertFalse(order.clearance_date_backup, "the backup is consumed once restored")

    def test_no_clearance_timestamp_blocks_picking_validation(self):
        """A hard lock only ever earns the right to HOLD stock granted some
        other way — it must never authorize completing a physical
        Pick/Ship transfer for an order that has never actually joined the
        queue via payment or grace period."""
        self._set_stock(10)
        order = self._make_admin_only_order(self.partner_a, 10)
        order.order_line.move_ids._action_assign()
        order.write({"is_reservation_hard_locked": True})

        pick = order.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))
        self.assertTrue(pick)
        with self.assertRaises(UserError):
            pick.button_validate()

    def test_pick_fully_validated_freeze_condition(self):
        """The hard-lock/force-reserve toggle must stay editable while a
        backorder is still open, and freeze only once the Pick step is
        completely done with nothing outstanding."""
        self._set_stock(1)
        order = self._make_order(self.partner_a, 5)
        pick = order.picking_ids.filtered(
            lambda p: p.picking_type_id == order.warehouse_id.pick_type_id
        )
        move = pick.move_ids
        self.assertEqual(move.quantity, 1)
        for move_line in move.move_line_ids:
            move_line.quantity = 1
        res = pick.button_validate()
        if isinstance(res, dict) and res.get("res_model") == "stock.backorder.confirmation":
            self.env["stock.backorder.confirmation"].with_context(**res["context"]).create({}).process()

        self.assertFalse(order.pick_fully_validated, "a backorder is still open")

        product2 = self.env["product.product"].create({
            "name": "Test Clearance Widget 2", "is_storable": True,
        })
        self._set_stock(10, product=product2)
        order2 = self._make_order(self.partner_b, 5, product=product2)
        pick2 = order2.picking_ids.filtered(
            lambda p: p.picking_type_id == order2.warehouse_id.pick_type_id
        )
        pick2.button_validate()
        self.assertTrue(order2.pick_fully_validated, "fully done, no backorder left open")

    def test_hard_lock_release_clears_ship_leg_lock_too(self):
        """Bug: releasing the order-level hard lock only looked at
        _get_pick_moves() (Pick-type only), so a Ship-leg move that the
        same hard lock had flagged is_locked_reservation=True on stayed
        stuck soft-locked forever after the lock was released.

        Uses a normally-confirmed order (real clearance_date via the
        grace_period window) so the Pick can actually be validated — the
        hard lock is layered on TOP of that, exercising the "both
        hard-locked and genuinely queued" combination, which still counts
        as tier 0 and still needs its Ship-leg flag cleared on release.
        """
        self._set_stock(10)
        order = self._make_order(self.partner_a, 10)
        order.write({"is_reservation_hard_locked": True})
        self.env["sale.order"]._reserve_by_clearance(product_ids=[self.product.id])

        pick = order.picking_ids.filtered(
            lambda p: p.picking_type_id == order.warehouse_id.pick_type_id
        )
        pick.button_validate()

        ship_move = order.order_line.move_ids.filtered(lambda m: m.state not in ("done", "cancel"))
        self.assertTrue(ship_move, "Ship leg should still be open")

        order.write({"is_reservation_hard_locked": False})
        ship_move.invalidate_recordset()
        self.assertFalse(
            ship_move.is_locked_reservation,
            "release must clear the lock flag on the Ship leg too, not just Pick-type moves",
        )

    def test_far_future_orders_excluded_unless_overridden(self):
        """Explicit product decision: an order scheduled more than 6 months
        out has no claim on today's scarce stock, even if it would
        otherwise win by clearance timestamp — unless it's hard-locked or
        force-reserved, in which case the override sticks regardless of
        how far out the order is scheduled."""
        self._set_stock(10)
        order_far = self._make_order(self.partner_a, 10)
        order_far.pick_scheduled_date = fields.Datetime.now() + relativedelta(months=8)
        self.assertEqual(order_far.order_line.clearance_defer_reason, "Scheduled Far Out")

        order_near = self._make_order(self.partner_b, 10)
        # order_far's clearance_date is earlier (created first) and would
        # normally win the stock — its far-future schedule excludes it.
        self.env["sale.order"]._reserve_by_clearance(product_ids=[self.product.id])
        order_far.order_line.invalidate_recordset()
        order_near.order_line.invalidate_recordset()

        self.assertEqual(order_far.order_line.move_ids.quantity, 0, "excluded for being scheduled too far out")
        self.assertEqual(order_near.order_line.move_ids.quantity, 10, "gets it instead, despite the later clearance")

        # Hard-locking it overrides the exclusion entirely.
        order_far.write({"is_reservation_hard_locked": True})
        self.assertFalse(order_far.order_line.clearance_defer_reason, "override clears the defer tag too")
        self._set_stock(20)
        self.env["sale.order"]._reserve_by_clearance(product_ids=[self.product.id])
        order_far.order_line.invalidate_recordset()
        self.assertEqual(order_far.order_line.move_ids.quantity, 10, "hard lock overrides the far-future exclusion")

    def test_manual_stage_override_sticks_regardless_of_payment(self):
        """Explicit product decision: the manual Override Stage field must
        be able to force ANY stage, including Ship, on an unpaid order —
        and it must actually stick, not get silently reverted by the
        reservation engine's own generic re-runs. Ship/order_pick can only
        ever be changed by a genuine payment event afterward (see
        account_move.py), never as a side effect of something unrelated
        (a quant update, a lock toggle on a different order) re-running
        _reserve_by_clearance."""
        self._set_stock(10)
        order = self._make_admin_only_order(self.partner_a, 10)
        self.assertFalse(order._is_fully_paid())

        order.write({"fulfillment_stage": "ship"})
        self.assertEqual(order.fulfillment_stage, "ship", "override must apply even though the order isn't paid")

        # Triggering the reservation engine for an unrelated reason must
        # never fight the override.
        self.env["sale.order"]._reserve_by_clearance(product_ids=[self.product.id])
        self.assertEqual(
            order.fulfillment_stage, "ship",
            "an unrelated _reserve_by_clearance run must never revert a manual override",
        )

        # A genuine payment event is still the one thing that can move it —
        # paying it in full keeps it at ship (already there); confirms the
        # payment hook itself doesn't choke on an order already overridden
        # into place.
        self._pay_order(order)
        self.assertEqual(order.fulfillment_stage, "ship")

    def test_grace_period_clearance_is_confirmation_time_and_survives_graduation(self):
        """Explicit product decision: a grace-period order's clearance
        timestamp is the actual moment action_confirm() runs, not the
        order's create_date — a quotation that sat around for a while
        before being confirmed gets its place in line from when it was
        ACTUALLY confirmed, not from when it was first drafted. Graduating
        to order_pick via genuine payment while still in grace_period must
        leave that exact timestamp untouched, not restart the clock."""
        self._set_stock(10)
        order = self.env["sale.order"].create({
            "partner_id": self.partner_a.id,
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 10})],
        })
        # Simulate a quotation that sat around for a while before being
        # confirmed — create_date isn't writable via the ORM, so backdate
        # it directly.
        backdated_create = fields.Datetime.now() - timedelta(days=10)
        self.env.cr.execute(
            "UPDATE sale_order SET create_date = %s WHERE id = %s",
            (backdated_create, order.id),
        )
        order.invalidate_recordset(["create_date"])
        self.assertEqual(order.create_date, backdated_create)

        order.action_confirm()
        self.assertEqual(order.fulfillment_stage, "grace_period")
        self.assertNotEqual(
            order.clearance_date, backdated_create,
            "must stamp the actual confirmation moment, not the (possibly stale) create_date",
        )
        confirmation_clearance = order.clearance_date
        self.assertAlmostEqual(
            confirmation_clearance, fields.Datetime.now(), delta=timedelta(seconds=5),
        )

        self._pay_order(order)
        self.assertEqual(
            order.clearance_date, confirmation_clearance,
            "graduating out of grace_period via real payment must not touch the confirmation timestamp",
        )

    def test_grace_period_expiry_starts_fresh_on_later_payment(self):
        """Losing the grace-period window is the opposite of a full
        refund: no backup is kept, so if the order is genuinely paid
        later, it gets a brand-new timestamp reflecting its real (much
        later) place in line — not its stale original one."""
        self._set_stock(10)
        order = self._make_order(self.partner_a, 10)

        # Simulate the grace period having expired.
        order.with_context(clearance_internal_write=True).write({
            "clearance_date": fields.Datetime.now() - timedelta(days=GRACE_PERIOD_DAYS + 1),
        })
        self.env["sale.order"]._cron_expire_grace_period()
        self.assertEqual(order.fulfillment_stage, "no_invoice")
        self.assertFalse(order.clearance_date)
        self.assertFalse(order.clearance_date_backup, "expiry must not preserve the old timestamp for later restoration")

        # With no backup, _resolve_clearance_date must pass the fresh
        # timestamp straight through rather than reaching for anything
        # stale — checked directly rather than by comparing wall-clock
        # values, which a fast test can't reliably tell apart from the
        # original (both land in the same ORM flush).
        sentinel = fields.Datetime.now()
        self.assertEqual(order._resolve_clearance_date(sentinel), sentinel)

        self._pay_order(order)
        self.assertTrue(order.clearance_date)

    def test_override_stamps_a_flagged_timestamp_that_real_payment_replaces(self):
        """Explicit product decision: overriding into grace_period/
        order_pick/ship DOES stamp a clearance_date — the order must act
        exactly as if it had genuinely cleared (competes at its rightful
        tier, can validate a Pick/Ship transfer). That timestamp is
        flagged clearance_is_override, purely for display, since it's
        never real payment. The moment genuine payment actually arrives,
        it REPLACES the fabricated timestamp with the real one and clears
        the flag — an override is a stand-in, not something that
        outlives real payment showing up."""
        self._set_stock(10)
        order = self._make_admin_only_order(self.partner_a, 10)
        self.assertFalse(order.clearance_date)

        order.write({"fulfillment_stage": "ship"})
        self.assertTrue(order.clearance_date, "the override must stamp a timestamp")
        self.assertTrue(order.clearance_is_override)
        override_timestamp = order.clearance_date

        # It genuinely competes for stock at its (override-stamped) tier,
        # exactly like a real clearance would.
        self.env["sale.order"]._reserve_by_clearance(product_ids=[self.product.id])
        order.order_line.invalidate_recordset()
        self.assertEqual(order.order_line.move_ids.quantity, 10, "an override-cleared order competes for stock normally")

        # Real payment arriving now must replace the fabricated timestamp
        # with the genuine one and clear the override flag — never keep
        # showing the fabricated one as if it were real. Pinned with a
        # controlled, distinct sentinel rather than wall-clock timing:
        # fields.Datetime.now() truncates to whole seconds, so a fast
        # test can easily land the override and the "real" timestamp in
        # the very same second and make them indistinguishable.
        sentinel = fields.Datetime.now() + relativedelta(years=5)
        with patch(
            "odoo.addons.clearance_reservation.models.account_move.AccountMove._get_clearance_timestamp",
            return_value=sentinel,
        ):
            self._pay_order(order)
            # Read (and thus force any pending compute/flush) while the
            # patch is still active — payment_state's recompute can be
            # deferred to a later flush point, which must not land after
            # the mock has already been torn down.
            self.assertFalse(order.clearance_is_override, "genuine payment must clear the override flag")
            self.assertNotEqual(order.clearance_date, override_timestamp, "must not keep the fabricated timestamp")
            self.assertEqual(
                order.clearance_date, sentinel,
                "genuine payment must replace the fabricated timestamp with the real one",
            )
        self.assertEqual(order.fulfillment_stage, "ship", "already-elevated stage must not be downgraded by real payment")

    def test_override_to_no_invoice_backs_up_and_clears_override_flag(self):
        self._set_stock(10)
        order = self._make_admin_only_order(self.partner_a, 10)
        order.write({"fulfillment_stage": "order_pick"})
        stamped = order.clearance_date
        self.assertTrue(order.clearance_is_override)

        order.write({"fulfillment_stage": "no_invoice"})
        self.assertFalse(order.clearance_date)
        self.assertFalse(order.clearance_is_override)
        self.assertEqual(order.clearance_date_backup, stamped)

    def test_scheduled_future_stock_requires_a_committed_po_not_just_any_incoming_move(self):
        """An incoming move with no purchase order behind it at all — or
        one behind a draft/RFQ rather than a genuinely committed PO — must
        never be trusted as the safety net that justifies giving up
        currently-held stock. Only state 'purchase'/'done' counts."""
        self._set_stock(10)
        order = self._make_order(self.partner_a, 10)
        scheduled = fields.Datetime.now() + relativedelta(days=30)
        order.pick_scheduled_date = scheduled

        self.env["stock.move"].create({
            "name": "Manual incoming, no PO behind it",
            "product_id": self.product.id,
            "product_uom_qty": 10,
            "product_uom": self.product.uom_id.id,
            "location_id": self.env.ref("stock.stock_location_suppliers").id,
            "location_dest_id": self.warehouse.lot_stock_id.id,
            "picking_type_id": self.warehouse.in_type_id.id,
            "date": scheduled - timedelta(days=14),
        })._action_confirm()

        self.env.invalidate_all()
        self.assertFalse(
            order.order_line.clearance_defer_reason,
            "an incoming move with no committed PO behind it must never count as a safety net",
        )

        self._create_committed_po(10, scheduled - timedelta(days=14))
        self.env.invalidate_all()
        self.assertEqual(
            order.order_line.clearance_defer_reason, "Scheduled Future Stock",
            "a genuinely committed PO covering the same demand does qualify",
        )

    def test_scheduled_future_stock_requires_enough_buffer_before_release(self):
        """The matched incoming shipment must land at least
        SCHEDULED_FUTURE_STOCK_RELEASE_BUFFER_DAYS ahead of the order's own
        scheduled date — a real safety margin, not a hair's breadth."""
        self._set_stock(10)
        order = self._make_order(self.partner_a, 10)
        scheduled = fields.Datetime.now() + relativedelta(days=30)
        order.pick_scheduled_date = scheduled

        # Only 5 days of buffer — short of the required 14.
        self._create_committed_po(10, scheduled - timedelta(days=5))
        self.env.invalidate_all()
        self.assertFalse(
            order.order_line.clearance_defer_reason,
            "an incoming shipment cutting it too close must not count as a safe replacement",
        )

    def test_scheduled_future_stock_releases_to_earlier_scheduled_order_and_reclaims_ahead_of_force_reserve(self):
        """The core safety guarantee, end to end: a line that gives up its
        stock under this mechanism must (a) actually hand it to the
        earlier-scheduled competitor it exists to help, (b) stay flagged
        for elevated reclaim priority even while it holds nothing at all,
        and (c) win the very incoming shipment that justified giving it
        up ahead of a force-reserve that shows up wanting the same
        product — through BOTH the ordinary allocation pass AND the
        targeted release pass, since a force-reserve must never be able
        to steal a released holder's promised replacement through either
        path.
        """
        self._set_stock(10)
        holder = self._make_order(self.partner_a, 10)
        holder_scheduled = fields.Datetime.now() + relativedelta(days=30)
        holder.pick_scheduled_date = holder_scheduled
        holder.order_line.invalidate_recordset()
        self.assertEqual(holder.order_line.move_ids.quantity, 10, "holds all the current stock")

        po = self._create_committed_po(10, holder_scheduled - timedelta(days=14))
        self.env.invalidate_all()
        self.assertEqual(holder.order_line.clearance_defer_reason, "Scheduled Future Stock")

        # An earlier-scheduled competitor. Note: action_confirm()'s own
        # write hook re-runs the queue immediately on creation (using
        # whatever default scheduled date sale_stock assigned at that
        # point, already earlier than the holder's 30-days-out schedule),
        # so the release below may already have happened synchronously —
        # the explicit call just makes it deterministic and covers the
        # case where it hasn't.
        early = self._make_order(self.partner_b, 10)
        early.pick_scheduled_date = fields.Datetime.now() + relativedelta(days=5)

        self.env["sale.order"]._reserve_by_clearance(product_ids=[self.product.id])
        holder.order_line.invalidate_recordset()
        early.order_line.invalidate_recordset()

        self.assertEqual(early.order_line.move_ids.quantity, 10, "the earlier-scheduled order gets the released stock")
        self.assertEqual(holder.order_line.move_ids.quantity, 0, "the holder gave up its stock")
        self.assertTrue(
            holder.order_line.is_scheduled_future_stock_release,
            "the holder must stay flagged even while holding nothing at all",
        )
        self.assertEqual(
            holder.order_line.clearance_defer_reason, "Scheduled Future Stock",
            "the tag must survive giving up its stock entirely",
        )

        # Hard-lock early's line now that it holds its rightful stock —
        # protects it from the blanket clearance-tier reallocation that
        # runs on every _reserve_by_clearance call. Without this, early's
        # ordinary (tier-2) reservation would itself get swept up and
        # reallocated the next time the queue runs (unrelated to this
        # mechanism — force-reserve has always outranked ordinary
        # clearance-tier priority for stock that isn't already spoken
        # for, confirmed by
        # test_force_reserve_outranks_payment_but_hard_lock_grants_none),
        # which would muddy this test's actual target: whether
        # force-reserve can steal the SPECIFIC shipment earmarked for the
        # released holder.
        early.order_line.write({"is_reservation_hard_locked": True})

        # A force-reserve now shows up wanting the SAME product, right
        # when the promised future shipment actually arrives.
        late_force = self._make_admin_only_order(self.partner_c, 10)
        late_force.order_line.write({"is_force_reserved": True})

        # stock_picking.py's own button_validate() override already
        # re-runs _reserve_by_clearance for a completed receipt — no
        # separate explicit call here. That matters: once this reclaims
        # the holder's stock, it's an ordinary (tier-2) reservation again
        # (the safety net having done its job), and — same as any other
        # ordinary reservation, per the module's pre-existing, long-since
        # established rules — a LATER _reserve_by_clearance run could
        # legitimately still hand it to a force-reserve, exactly like
        # test_force_reserve_outranks_payment_but_hard_lock_grants_none
        # already covers. That's a separate, general, accepted risk, not
        # a hole in this mechanism specifically — this test only claims
        # the guarantee holds for winning the shipment as it arrives.
        po.picking_ids.button_validate()
        holder.order_line.invalidate_recordset()
        late_force.order_line.invalidate_recordset()

        self.assertEqual(
            holder.order_line.move_ids.filtered(lambda m: m.state not in ("done", "cancel")).quantity, 10,
            "the holder reclaims the arriving shipment it was promised, ahead of the force-reserve",
        )
        self.assertEqual(
            late_force.order_line.move_ids.quantity, 0,
            "force-reserve must not be able to steal the shipment earmarked for the released holder",
        )
        self.assertFalse(
            holder.order_line.is_scheduled_future_stock_release,
            "fully reclaimed — the flag must clear once the safety net has done its job",
        )

    def test_scheduled_future_stock_flag_clears_if_safety_net_evaporates(self):
        """If the committed PO backing a released holder's promise is
        cancelled before the holder reclaims anything, the persistent flag
        must not keep granting elevated priority forever with nothing left
        actually backing it — it falls back to ordinary clearance-date
        priority instead."""
        self._set_stock(10)
        holder = self._make_order(self.partner_a, 10)
        holder_scheduled = fields.Datetime.now() + relativedelta(days=30)
        holder.pick_scheduled_date = holder_scheduled

        po = self._create_committed_po(10, holder_scheduled - timedelta(days=14))
        self.env.invalidate_all()
        self.assertEqual(holder.order_line.clearance_defer_reason, "Scheduled Future Stock")

        early = self._make_order(self.partner_b, 10)
        early.pick_scheduled_date = fields.Datetime.now() + relativedelta(days=5)

        self.env["sale.order"]._reserve_by_clearance(product_ids=[self.product.id])
        holder.order_line.invalidate_recordset()
        self.assertEqual(holder.order_line.move_ids.quantity, 0, "the holder gave up its stock")
        self.assertTrue(holder.order_line.is_scheduled_future_stock_release)

        # The promised future shipment is cancelled before it ever arrives.
        po.button_cancel()
        self.env.invalidate_all()

        self.env["sale.order"]._reserve_by_clearance(product_ids=[self.product.id])
        holder.order_line.invalidate_recordset()
        self.assertFalse(
            holder.order_line.is_scheduled_future_stock_release,
            "the flag must clear once the safety net backing it no longer exists",
        )
        self.assertFalse(
            holder.order_line.clearance_defer_reason,
            "with nothing backing it any more, the line falls back to ordinary priority",
        )

    def test_ineligible_order_never_shields_foreign_stock_via_scheduled_future_stock(self):
        """Bug found via live data: an order that qualified for
        "Scheduled Future Stock" while genuinely eligible (e.g.
        grace_period) kept that tag — and the protection from reclaim it
        grants — even after losing eligibility entirely (manual override
        to no_invoice, a full refund, or grace-period expiry). Since the
        tag shields a line from the blanket reclaim in
        _reserve_by_clearance, a no_invoice order with zero real claim
        could indefinitely squat on stock it's not entitled to, purely by
        coincidentally matching a future incoming shipment — directly
        contradicting the module's foundational rule that a no_invoice
        order (not hard-locked, not force-reserved) has no legitimate
        claim on stock at all.
        """
        self._set_stock(10)
        order = self._make_order(self.partner_a, 10)
        scheduled = fields.Datetime.now() + relativedelta(days=30)
        order.pick_scheduled_date = scheduled
        order.order_line.invalidate_recordset()
        self.assertEqual(order.order_line.move_ids.quantity, 10)

        self._create_committed_po(10, scheduled - timedelta(days=14))
        self.env.invalidate_all()
        self.assertEqual(order.order_line.clearance_defer_reason, "Scheduled Future Stock")

        # Loses eligibility entirely — manual override to no_invoice.
        order.write({"fulfillment_stage": "no_invoice"})
        self.env.invalidate_all()
        self.assertFalse(
            order.order_line.clearance_defer_reason,
            "a no_invoice order must never keep this tag, regardless of any matching future PO",
        )

        # An ordinary competing order must be able to reclaim the stock —
        # the now-ineligible order has no more protection than any other
        # no_invoice/foreign holder.
        competitor = self._make_order(self.partner_b, 10)
        self.env["sale.order"]._reserve_by_clearance(product_ids=[self.product.id])
        order.order_line.invalidate_recordset()
        competitor.order_line.invalidate_recordset()
        self.assertEqual(order.order_line.move_ids.quantity, 0, "the ineligible order must give up its stock")
        self.assertEqual(competitor.order_line.move_ids.quantity, 10, "a genuinely eligible order reclaims it")

    def test_scheduled_future_stock_badge_hidden_when_fully_held(self):
        """Found via a live-data question: a line fully holding its
        demand still showed the "Scheduled Future Stock" badge just
        because it was eligible to give some of it up later — confusing,
        since nothing is actually pending. The underlying protection
        (move_priority tier, the "protected" exclusion from the blanket
        release) must survive regardless of current state, since an
        earlier-scheduled competitor still shouldn't be able to grab it
        via the ordinary clearance-date reallocation — only the
        user-facing badge should hide while nothing is genuinely short.
        """
        self._set_stock(10)
        order = self._make_order(self.partner_a, 10)
        scheduled = fields.Datetime.now() + relativedelta(days=30)
        order.pick_scheduled_date = scheduled
        order.order_line.invalidate_recordset()
        self.assertEqual(order.order_line.move_ids.quantity, 10, "fully held")

        self._create_committed_po(10, scheduled - timedelta(days=14))
        self.env.invalidate_all()
        self.assertEqual(
            order.order_line.clearance_defer_reason, "Scheduled Future Stock",
            "the underlying protection still applies even while fully held",
        )
        move = order.order_line.move_ids.filtered(lambda m: m.state not in ("done", "cancel"))
        self.assertEqual(move.state, "assigned")
        self.assertFalse(
            move.clearance_lock_reason,
            "the badge must not show while the line is fully held — nothing is actually pending",
        )

        # Give some of it up to an earlier-scheduled competitor — now
        # genuinely short, the badge should show.
        early = self._make_order(self.partner_b, 10)
        early.pick_scheduled_date = fields.Datetime.now() + relativedelta(days=5)
        self.env["sale.order"]._reserve_by_clearance(product_ids=[self.product.id])
        order.order_line.invalidate_recordset()
        move.invalidate_recordset()
        self.assertEqual(order.order_line.move_ids.quantity, 0, "gave up its stock")
        self.assertEqual(
            move.clearance_lock_reason, "Scheduled Future Stock",
            "now genuinely short and waiting — the badge must show",
        )

    def test_scheduled_future_stock_badge_hidden_on_reserved_portion_of_split_line(self):
        """Found via a live-data question: a partially-fulfilled
        "Scheduled Future Stock" line produces TWO forecast report lines
        for the same move — the already-secured chunk (reservation
        truthy) and the still-unfulfilled remainder (reservation falsy).
        The badge means "still waiting on something," true only of the
        remainder; the secured chunk already has what it needs. This is
        the same principle as the fully-held case, but that fix (keyed
        off the move's own state) doesn't catch it, since a partially-
        available move's state is never "assigned"."""
        self._set_stock(6)
        order = self._make_order(self.partner_a, 10)
        scheduled = fields.Datetime.now() + relativedelta(days=30)
        order.pick_scheduled_date = scheduled
        order.order_line.invalidate_recordset()
        self.assertEqual(order.order_line.move_ids.quantity, 6, "partially fulfilled")

        self._create_committed_po(10, scheduled - timedelta(days=14))
        self.env.invalidate_all()
        self.assertEqual(order.order_line.clearance_defer_reason, "Scheduled Future Stock")

        report = self.env["stock.forecasted_product_product"].with_context(warehouse=self.warehouse.id)
        data = report._get_report_data(product_ids=[self.product.id])
        lines_for_order = [
            l for l in data["lines"]
            if l.get("document_out") and l["document_out"].get("id") == order.id
        ]
        self.assertEqual(
            len(lines_for_order), 2,
            "expected the reserved chunk and the waiting remainder as separate lines",
        )

        reserved_line = next(l for l in lines_for_order if l.get("reservation"))
        waiting_line = next(l for l in lines_for_order if not l.get("reservation"))
        self.assertEqual(reserved_line["quantity"], 6)
        self.assertFalse(
            reserved_line.get("lock_reason"),
            "the already-secured chunk must not show the badge",
        )
        self.assertEqual(
            waiting_line.get("lock_reason"), "Scheduled Future Stock",
            "the still-waiting remainder must show the badge",
        )

    def test_buffer_replenishment_reserves_order_when_picking_empty(self):
        """Explicit product decision: reservation must succeed as long as
        stock exists ANYWHERE in the warehouse, not just Picking Zone —
        via an instant, already-validated internal transfer, never a
        pending one left for a human to validate first. The Pick move
        itself must still only ever source from Picking Zone."""
        self._set_buffer_stock(10)
        order = self._make_order(self.partner_a, 10)
        move = order.order_line.move_ids
        self.assertEqual(
            move.state, "assigned",
            "should be fully reserved via instant buffer top-up",
        )
        self.assertEqual(
            move.location_id, self.pick_source_location,
            "the Pick move itself must still only ever source from Picking Zone",
        )
        # Scoped to this test's own product — the suite runs against a
        # shared dev database that can carry replenishment pickings from
        # unrelated products (e.g. earlier live verification), which an
        # unscoped search would wrongly pick up too.
        replenishment = self.env["stock.picking"].search([
            ("is_clearance_replenishment", "=", True),
            ("move_ids.product_id", "=", self.product.id),
        ])
        self.assertEqual(len(replenishment), 1, "expected exactly one auto-generated replenishment transfer")
        self.assertEqual(
            replenishment.state, "done",
            "must be immediately validated, not left pending for a human",
        )

    def test_buffer_replenishment_moves_exact_shortfall_only(self):
        """Only the actual shortfall moves — Picking already has some
        stock, Buffer has plenty more; must top up only what's needed."""
        self._set_stock(4)
        self._set_buffer_stock(50)
        order = self._make_order(self.partner_a, 10)
        move = order.order_line.move_ids
        self.assertEqual(move.state, "assigned")
        replenishment_moves = self.env["stock.move"].search([
            ("location_id", "=", self.buffer_location.id),
            ("location_dest_id", "=", self.pick_source_location.id),
        ])
        self.assertEqual(
            sum(replenishment_moves.mapped("product_uom_qty")), 6,
            "only the 6-unit shortfall (10 needed - 4 already at Picking) should move",
        )

    def test_buffer_replenishment_insufficient_combined_stock_leaves_genuine_shortfall(self):
        """Picking + Buffer combined still can't cover demand — no
        over-creation, and the line is correctly left genuinely short,
        exactly as it would be without this feature at all."""
        self._set_stock(2)
        self._set_buffer_stock(3)
        order = self._make_order(self.partner_a, 10)
        move = order.order_line.move_ids
        self.assertEqual(move.state, "partially_available")
        self.assertEqual(move.quantity, 5, "reserved everything available (2 + 3), still short of 10")
        replenishment = self.env["stock.picking"].search([
            ("is_clearance_replenishment", "=", True),
            ("move_ids.product_id", "=", self.product.id),
        ])
        self.assertEqual(len(replenishment), 1, "one replenishment attempt, no runaway duplicate creation")

    def test_relocating_reserved_quant_reclaims_reservation_via_auto_replenishment(self):
        """Odoo's native "Relocate" quant action (stock.quant.move_quants,
        the method behind the Inventory > Reporting > Locations wizard)
        can move already-reserved stock with no error — but as a side
        effect it silently drops the existing reservation. Relocating
        reserved Picking Zone stock into Buffer Zone must not leave the
        order stranded: the module's own move_quants override re-runs the
        queue immediately, which — via the same instant top-up as any
        other shortfall — pulls the stock straight back into Picking Zone
        and re-reserves it, all within this one call."""
        self._set_stock(10)
        order = self._make_order(self.partner_a, 10)
        move = order.order_line.move_ids
        self.assertEqual(move.state, "assigned")

        quant = self.env["stock.quant"]._gather(self.product, self.pick_source_location)
        quant.move_quants(location_dest_id=self.buffer_location)

        move.invalidate_recordset()
        self.assertEqual(
            move.state, "assigned",
            "relocation drops the old reservation, but the auto-replenishment "
            "triggered by move_quants' own override should have already pulled "
            "the stock back into Picking Zone and re-reserved it",
        )
        self.assertEqual(move.location_id, self.pick_source_location)

    def test_buffer_replenishment_combines_multiple_products_into_one_transfer(self):
        """Two different products both needing a top-up in the SAME pass
        of _reserve_by_clearance must land on ONE combined transfer, not
        a separate one per product — a single document for a warehouse
        staffer to act on. Stock is set via the low-level quant API
        directly (bypassing _apply_inventory's own per-call reservation
        trigger) specifically so both shortfalls are only ever addressed
        by the single explicit _reserve_by_clearance() call below, not by
        two separate triggered passes."""
        other_product = self.env["product.product"].create({
            "name": "Test Clearance Widget B",
            "is_storable": True,
        })
        order_a = self._make_order(self.partner_a, 10)
        order_b = self._make_order(self.partner_b, 5, product=other_product)
        self.assertNotEqual(order_a.order_line.move_ids.state, "assigned", "no stock yet")
        self.assertNotEqual(order_b.order_line.move_ids.state, "assigned", "no stock yet")

        self.env["stock.quant"]._update_available_quantity(self.product, self.buffer_location, 10)
        self.env["stock.quant"]._update_available_quantity(other_product, self.buffer_location, 5)
        self.env["sale.order"]._reserve_by_clearance()

        replenishments = self.env["stock.picking"].search([
            ("is_clearance_replenishment", "=", True),
            ("move_ids.product_id", "in", [self.product.id, other_product.id]),
        ])
        self.assertEqual(
            len(replenishments), 1,
            "both products' shortfalls should combine into a single transfer",
        )
        self.assertEqual(len(replenishments.move_ids), 2, "one move per product on that transfer")

    def test_buffer_replenishment_preserves_priority_between_competing_orders(self):
        """Buffer only covers ONE of two competing orders' demand — the
        top-up must not disturb the existing priority ordering: whichever
        order actually has priority gets fully reserved, the other stays
        short, exactly as it would with an ordinary (non-buffer) shortage."""
        self._set_buffer_stock(10)
        earlier = self._make_order(self.partner_a, 10)
        later = self._make_order(self.partner_b, 10)
        earlier.order_line.move_ids.invalidate_recordset()
        later.order_line.move_ids.invalidate_recordset()
        self.assertEqual(
            earlier.order_line.move_ids.state, "assigned",
            "earlier-cleared order should win the only 10 units the buffer had",
        )
        self.assertEqual(
            later.order_line.move_ids.state, "confirmed",
            "later order should still be genuinely short — the top-up must not "
            "manufacture stock beyond what buffer actually had",
        )

    def test_buffer_replenishment_skipped_for_far_future_order(self):
        """An order scheduled more than FAR_FUTURE_MONTHS out has no
        business holding scarce stock today (existing rule) — it must
        also never trigger a buffer replenishment on Buffer's behalf,
        which would otherwise move real stock for no legitimate demand.
        Scheduled far-future BEFORE any stock exists anywhere, so the
        order never gets a chance to legitimately reserve (and trigger a
        top-up) before becoming far-future — otherwise this would just be
        testing that an already-topped-up order keeps what it has, not
        that a far-future one is denied a NEW top-up."""
        order = self._make_order(self.partner_a, 10)
        order.pick_scheduled_date = fields.Datetime.now() + relativedelta(months=7)
        self._set_buffer_stock(10)

        replenishments = self.env["stock.picking"].search([
            ("is_clearance_replenishment", "=", True),
            ("move_ids.product_id", "=", self.product.id),
        ])
        self.assertFalse(
            replenishments,
            "a far-future, non-overridden order must never trigger a buffer top-up",
        )

    def test_buffer_replenishment_skipped_when_buffer_zone_not_configured(self):
        """Left unconfigured, the feature must no-op completely — a
        shortfall waits exactly as it did before this existed."""
        self.warehouse.buffer_zone_location_id = False
        self._set_buffer_stock(10)
        order = self._make_order(self.partner_a, 10)
        move = order.order_line.move_ids
        self.assertNotEqual(move.state, "assigned", "must not reserve from an unconfigured buffer")
        replenishments = self.env["stock.picking"].search([
            ("is_clearance_replenishment", "=", True),
            ("move_ids.product_id", "=", self.product.id),
        ])
        self.assertFalse(replenishments)

    def test_buffer_replenishment_covers_a_force_reserved_line(self):
        """The top-up isn't limited to ordinary clearance-tier demand —
        a force-reserved line short on Picking Zone stock, with enough in
        Buffer, must be topped up and reserved too."""
        self._set_buffer_stock(10)
        order = self._make_admin_only_order(self.partner_a, 10)
        order.order_line.is_force_reserved = True
        order.order_line.move_ids.invalidate_recordset()
        self.assertEqual(order.order_line.move_ids.state, "assigned")
        replenishments = self.env["stock.picking"].search(
            [("is_clearance_replenishment", "=", True)]
        )
        self.assertTrue(replenishments)

    def test_relocate_blocked_for_hard_locked_reservation(self):
        """Relocating a hard-locked reservation must be refused outright,
        with no override — mirrors _do_unreserve's own unconditional
        refusal exactly, since Relocate is otherwise a silent bypass of
        that same guarantee."""
        self._set_stock(10)
        order = self._make_order(self.partner_a, 10)
        order.is_reservation_hard_locked = True
        move = order.order_line.move_ids
        self.assertEqual(move.state, "assigned")

        other_location = self.env["stock.location"].create({
            "name": "Test Reorg Target",
            "location_id": self.warehouse.view_location_id.id,
            "usage": "internal",
        })
        quant = self.env["stock.quant"]._gather(self.product, self.pick_source_location)
        with self.assertRaises(UserError):
            quant.move_quants(location_dest_id=other_location)

        move.invalidate_recordset()
        self.assertEqual(move.state, "assigned", "the reservation must be completely untouched")

    def test_relocate_blocked_for_force_reserved_unless_overridden(self):
        """A force-reserved (soft-locked) reservation is refused by
        default, same as _do_unreserve — but CAN be bypassed via the same
        force_unreserve_override context every other override path in
        this module already honors."""
        self._set_stock(10)
        order = self._make_order(self.partner_a, 10)
        # action_force_reserve(), not a bare field write — the move is
        # already fully "assigned" from ordinary payment-tier reservation
        # at this point, and _reserve_by_clearance's fast path (nothing to
        # do when everything's already assigned) means a plain write
        # wouldn't reach the code that actually sets is_locked_reservation.
        order.order_line.action_force_reserve()
        move = order.order_line.move_ids
        move.invalidate_recordset()
        self.assertEqual(move.state, "assigned")
        self.assertTrue(move.is_locked_reservation)

        other_location = self.env["stock.location"].create({
            "name": "Test Reorg Target 2",
            "location_id": self.warehouse.view_location_id.id,
            "usage": "internal",
        })
        quant = self.env["stock.quant"]._gather(self.product, self.pick_source_location)
        with self.assertRaises(UserError):
            quant.move_quants(location_dest_id=other_location)
        move.invalidate_recordset()
        self.assertEqual(move.state, "assigned", "refused by default, exactly like _do_unreserve")

        quant.with_context(force_unreserve_override=True).move_quants(location_dest_id=other_location)
        move.invalidate_recordset()
        self.assertEqual(
            move.state, "confirmed",
            "explicitly overridden — relocation goes through same as an ordinary reservation",
        )
