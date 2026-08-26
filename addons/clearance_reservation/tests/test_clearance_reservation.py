from datetime import timedelta
from unittest.mock import patch

from dateutil.relativedelta import relativedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.clearance_reservation.models.sale_order import GRACE_PERIOD_DAYS
from odoo.addons.clearance_reservation.models.purchase_order import GOODS_TRANSIT_DAYS


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
    - the Pick move only ever sources from (and reserves out of) Picking
      Zone — Buffer Zone is never touched by this queue at all.
      Restocking Picking Zone from Buffer is handled entirely outside
      this module, by a native Odoo Reordering Rule + resupply route, so
      a real person always has to physically complete that transfer
      before the stock counts as available here — explicit product
      decision, after an earlier version of this feature that instantly
      "moved" stock in the database with nobody having physically moved
      anything, which broke trust in the warehouse's location data.
    - Odoo's native "Relocate" quant action can move already-reserved
      stock with no error, but silently drops the reservation as a side
      effect, and bypasses hard-lock/force-reserve protection entirely
      (unlike _do_unreserve/_action_cancel, which both correctly refuse).
      Fixed to mirror that same two-tier refusal, and to re-run the queue
      right after any allowed relocation so the affected order
      automatically reclaims whatever's still on hand for it.
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

    def _create_committed_po(self, qty, date_planned, product=None, confirmed=True):
        """confirmed=True (the default) means container_reference AND
        port_arrival_date are both filled in — is_receipt_confirmed, and
        so the tighter SCHEDULED_FUTURE_STOCK_RELEASE_BUFFER_DAYS margin.
        Every existing caller of this helper predates the confirmed/
        unconfirmed distinction and was written assuming the standard,
        reliable margin — defaulting to True here keeps every one of
        them meaning exactly what it always meant, rather than silently
        becoming an "unconfirmed" PO (SCHEDULED_FUTURE_STOCK_RELEASE_BUFFER_DAYS_UNCONFIRMED,
        a wider margin) the moment that distinction was introduced. Pass
        confirmed=False explicitly for a test that specifically wants an
        unconfirmed PO.

        port_arrival_date is deliberately back-solved from the requested
        date_planned (rather than always "today") — confirming a PO
        re-syncs date_planned/move.date to port_arrival_date +
        GOODS_TRANSIT_DAYS (see purchase_order.py's
        _sync_confirmed_receipt_date), so a caller's own date_planned
        would otherwise be silently overwritten to "today + transit
        time" the moment confirmed=True writes port_arrival_date, no
        matter what date_planned was asked for. Back-solving preserves
        every existing caller's actual intent (a shipment landing at
        exactly the requested date_planned) once confirmed."""
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
        if confirmed:
            po.write({
                "container_reference": "MSCU0000000",
                "port_arrival_date": fields.Datetime.to_datetime(date_planned).date()
                - timedelta(days=GOODS_TRANSIT_DAYS),
            })
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

        # Only 5 days of buffer — short of the required 7.
        self._create_committed_po(10, scheduled - timedelta(days=5))
        self.env.invalidate_all()
        self.assertFalse(
            order.order_line.clearance_defer_reason,
            "an incoming shipment cutting it too close must not count as a safe replacement",
        )

    def test_scheduled_future_stock_unconfirmed_po_requires_wider_buffer(self):
        """Explicit product decision: an UNCONFIRMED PO (no container
        reference / port arrival date on file — still just a planned
        date, not verified logistics data) needs
        SCHEDULED_FUTURE_STOCK_RELEASE_BUFFER_DAYS_UNCONFIRMED (30 days)
        of margin, not the tighter 7 days a confirmed shipment gets."""
        self._set_stock(10)
        order = self._make_order(self.partner_a, 10)
        scheduled = fields.Datetime.now() + relativedelta(days=60)
        order.pick_scheduled_date = scheduled

        # 20 days of buffer: comfortably clears the confirmed 7-day
        # margin, but falls short of the unconfirmed 30-day one.
        self._create_committed_po(10, scheduled - timedelta(days=20), confirmed=False)
        self.env.invalidate_all()
        self.assertFalse(
            order.order_line.clearance_defer_reason,
            "20 days isn't enough margin for an unconfirmed PO's still-merely-planned date",
        )

        # The exact same 20-day timing, but now confirmed, is enough —
        # port_arrival_date back-solved (same reasoning as
        # _create_committed_po) so confirming doesn't silently resync
        # date_planned/move.date to something else entirely.
        po = self.env["purchase.order"].search([("order_line.product_id", "=", self.product.id)])
        po.write({
            "container_reference": "MSCU7654321",
            "port_arrival_date": (scheduled - timedelta(days=20)).date() - timedelta(days=GOODS_TRANSIT_DAYS),
        })
        self.env.invalidate_all()
        self.assertEqual(
            order.order_line.clearance_defer_reason, "Scheduled Future Stock",
            "the same 20-day margin is enough once the shipment is confirmed",
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

    def test_scheduled_future_stock_never_overcommits_a_shared_shipment(self):
        """Bug found live: two lines can each individually pass
        _has_safe_future_replacement's per-line arithmetic against the
        exact SAME committed incoming shipment, since neither check knows
        about the other — both get tagged "Scheduled Future Stock", both
        give up their real stock to different earlier competitors, and
        when the shipment actually arrives (enough for only one of them)
        the other is left holding nothing, having released on a promise
        that was never really there for both. The safety check must be a
        GROUP decision per product: only as many lines as the shared pool
        can actually cover, in priority order, may ever be tagged."""
        self._set_stock(20)
        order_a = self._make_order(self.partner_a, 10)
        order_b = self._make_order(self.partner_b, 10)
        scheduled = fields.Datetime.now() + relativedelta(days=60)
        order_a.pick_scheduled_date = scheduled
        order_b.pick_scheduled_date = scheduled
        order_a.order_line.invalidate_recordset()
        order_b.order_line.invalidate_recordset()
        self.assertEqual(order_a.order_line.move_ids.quantity, 10)
        self.assertEqual(order_b.order_line.move_ids.quantity, 10)

        # A single committed PO comfortably covers EITHER order
        # individually — but not both at once.
        self._create_committed_po(10, scheduled - timedelta(days=20))
        self.env.invalidate_all()

        tagged = [
            line for line in (order_a.order_line, order_b.order_line)
            if line.clearance_defer_reason == "Scheduled Future Stock"
        ]
        self.assertEqual(
            len(tagged), 1,
            "only one of the two lines may be told it's safe to release — the "
            "shared 10-unit shipment can never cover both",
        )

        untagged = order_b.order_line if tagged[0] == order_a.order_line else order_a.order_line
        self.assertFalse(untagged.clearance_defer_reason)
        self.assertEqual(
            untagged.move_ids.filtered(lambda m: m.state not in ("done", "cancel")).quantity, 10,
            "the line NOT covered by the shared pool must keep holding its own real stock",
        )

    def test_scheduled_future_stock_group_check_covers_as_many_as_the_pool_allows(self):
        """Three equally-far-scheduled lines each independently believe a
        shared incoming pool covers their own 10-unit demand, but the
        pool only actually has 20 units — enough for two of them, not all
        three. Exactly two must be tagged, and the third must be left
        holding its own real stock, untagged."""
        self._set_stock(30)
        order_a = self._make_order(self.partner_a, 10)
        order_b = self._make_order(self.partner_b, 10)
        order_c = self._make_order(self.partner_c, 10)
        scheduled = fields.Datetime.now() + relativedelta(days=60)
        for order in (order_a, order_b, order_c):
            order.pick_scheduled_date = scheduled
            order.order_line.invalidate_recordset()
            self.assertEqual(order.order_line.move_ids.quantity, 10)

        self._create_committed_po(20, scheduled - timedelta(days=20))
        self.env.invalidate_all()

        all_lines = [order_a.order_line, order_b.order_line, order_c.order_line]
        tagged = [l for l in all_lines if l.clearance_defer_reason == "Scheduled Future Stock"]
        untagged = [l for l in all_lines if not l.clearance_defer_reason]
        self.assertEqual(len(tagged), 2, "a 20-unit pool can cover exactly two of the three 10-unit demands")
        self.assertEqual(len(untagged), 1)
        self.assertEqual(
            untagged[0].move_ids.filtered(lambda m: m.state not in ("done", "cancel")).quantity, 10,
            "the one line the pool can't cover must keep holding its own real stock",
        )

    def test_scheduled_future_stock_reclaim_not_stolen_by_ordinary_competitor(self):
        """Bug found live: once a released holder actually reclaims its
        promised shipment (the main allocation pass correctly gives it
        priority, tier 0), the SEPARATE targeted-release pass that runs
        right after must not treat that freshly-reclaimed stock as if it
        were just more of the holder's old, unclaimed current stock —
        otherwise it hands the holder's own just-won entitlement to a
        lower-tier (ordinary) competitor that has no claim on this
        shipment at all, leaving the tier-0 holder with nothing despite
        having legitimately won it moments earlier in the same run."""
        self._set_stock(10)
        holder = self._make_order(self.partner_a, 10)
        holder_scheduled = fields.Datetime.now() + relativedelta(days=60)
        holder.pick_scheduled_date = holder_scheduled
        holder.order_line.invalidate_recordset()
        self.assertEqual(holder.order_line.move_ids.quantity, 10)

        po = self._create_committed_po(10, holder_scheduled - timedelta(days=20))
        self.env.invalidate_all()
        self.assertEqual(holder.order_line.clearance_defer_reason, "Scheduled Future Stock")

        # Earlier-scheduled competitor triggers holder's FIRST release —
        # same mechanism as the existing reclaim test.
        early = self._make_order(self.partner_b, 10)
        early.pick_scheduled_date = fields.Datetime.now() + relativedelta(days=5)
        self.env["sale.order"]._reserve_by_clearance(product_ids=[self.product.id])
        holder.order_line.invalidate_recordset()
        early.order_line.invalidate_recordset()
        self.assertEqual(holder.order_line.move_ids.quantity, 0, "holder gave up its stock")
        self.assertEqual(early.order_line.move_ids.quantity, 10)
        early.order_line.write({"is_reservation_hard_locked": True})

        # A plain ORDINARY competitor (not force-reserved, not hard-locked
        # — just a normal, later-arriving order) is still genuinely
        # unfulfilled when the promised shipment actually lands.
        late_ordinary = self._make_order(self.partner_c, 10)

        po.picking_ids.button_validate()
        holder.order_line.invalidate_recordset()
        late_ordinary.order_line.invalidate_recordset()

        self.assertEqual(
            holder.order_line.move_ids.filtered(lambda m: m.state not in ("done", "cancel")).quantity, 10,
            "the holder must reclaim the shipment it was promised — the main pass already "
            "correctly gave it priority; the targeted-release pass must not undo that",
        )
        self.assertEqual(
            late_ordinary.order_line.move_ids.quantity, 0,
            "an ordinary competitor with no claim on this shipment must not receive the "
            "holder's just-reclaimed stock",
        )

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

    def test_forecast_uncleared_order_always_sorts_below_cleared_within_unreserved(self):
        """Found via a live queue question: within the unreserved block,
        an uncleared (no_invoice) order must never rank above a genuinely
        cleared (paid) order just because it's needed sooner — clearance
        status is a more fundamental signal than delivery date once
        you're comparing across cleared and uncleared, even though
        NEITHER currently holds any stock. An uncleared order has no
        clearance timestamp to sort by at all, so it falls back to
        delivery date purely as its own internal tiebreaker — see
        test_forecast_unreserved_lines_sort_by_clearance_not_delivery_date
        for the cleared side, which sorts by clearance timestamp instead."""
        cleared = self._make_order(self.partner_a, 10)
        cleared.pick_scheduled_date = fields.Datetime.now() + relativedelta(days=60)

        uncleared = self._make_admin_only_order(self.partner_b, 10)
        uncleared.pick_scheduled_date = fields.Datetime.now() + relativedelta(days=10)

        # Neither holds any stock — both stay fully unreserved.
        for order in (cleared, uncleared):
            order.order_line.move_ids.invalidate_recordset()
            self.assertNotEqual(order.order_line.move_ids.state, "assigned")

        report = self.env["stock.forecasted_product_product"].with_context(warehouse=self.warehouse.id)
        data = report._get_report_data(product_ids=[self.product.id])
        order_ids_in_order = [
            l["document_out"]["id"] for l in data["lines"]
            if l.get("document_out") and l["document_out"].get("_name") == "sale.order"
        ]
        self.assertLess(
            order_ids_in_order.index(cleared.id),
            order_ids_in_order.index(uncleared.id),
            "the cleared order must sort above the uncleared one despite its later delivery date",
        )

    def test_forecast_far_out_order_sorts_below_ordinary_but_above_uncleared(self):
        """Live question: S01471 (Scheduled Far Out, cleared months ago)
        was sorting ABOVE S01532 (an ordinary cleared order, cleared
        later) purely because its clearance_date happened to be
        earlier — even though the far-out order has NO real claim on
        anything right now (or for months), while the ordinary order
        genuinely competes. Being cleared doesn't matter once an order
        is excluded from the queue entirely for being scheduled too far
        out — it must sort below every line that's still actually
        competing. But it must still sort ABOVE an uncleared/no_invoice
        order — explicit product decision: having been genuinely cleared
        at some point, however excluded right now, still outranks never
        having had a real claim on stock at all."""
        self._set_stock(0)
        far_out = self._make_order(self.partner_a, 10)
        far_out.pick_scheduled_date = fields.Datetime.now() + relativedelta(months=8)
        self.assertEqual(far_out.order_line.clearance_defer_reason, "Scheduled Far Out")

        ordinary = self._make_order(self.partner_b, 10)
        ordinary.pick_scheduled_date = fields.Datetime.now() + relativedelta(days=30)
        # Explicit, distinct timestamps — far_out cleared FIRST (earlier
        # clearance_date), which would normally rank it first too.
        earlier = fields.Datetime.now() - timedelta(days=10)
        later = fields.Datetime.now() - timedelta(days=5)
        far_out.with_context(clearance_internal_write=True).write({"clearance_date": earlier})
        ordinary.with_context(clearance_internal_write=True).write({"clearance_date": later})

        uncleared = self._make_admin_only_order(self.partner_c, 10)
        uncleared.pick_scheduled_date = fields.Datetime.now() + relativedelta(days=15)

        for order in (far_out, ordinary, uncleared):
            order.order_line.move_ids.invalidate_recordset()
            self.assertNotEqual(order.order_line.move_ids.state, "assigned")

        report = self.env["stock.forecasted_product_product"].with_context(warehouse=self.warehouse.id)
        data = report._get_report_data(product_ids=[self.product.id])
        order_ids_in_order = [
            l["document_out"]["id"] for l in data["lines"]
            if l.get("document_out") and l["document_out"].get("_name") == "sale.order"
        ]
        self.assertLess(
            order_ids_in_order.index(ordinary.id),
            order_ids_in_order.index(far_out.id),
            "the far-out order must sort below the ordinary one despite its earlier clearance timestamp",
        )
        self.assertLess(
            order_ids_in_order.index(far_out.id),
            order_ids_in_order.index(uncleared.id),
            "the far-out order must still sort above an uncleared/no_invoice order",
        )

    def test_forecast_unreserved_lines_sort_by_clearance_not_delivery_date(self):
        """Explicit product decision: clearance timestamp is ALWAYS the
        leading signal for who gets stock next — an order's place in the
        queue is its place in the queue, whether or not it currently
        holds anything yet. A later-clearance order needed SOONER
        (earlier delivery date) must still sort BELOW an earlier-
        clearance order needed later, matching the order the real engine
        will actually satisfy them in once stock arrives — the display
        must mirror the real allocation order, not need-date urgency."""
        order_needed_soon = self._make_order(self.partner_a, 10)
        order_needed_soon.pick_scheduled_date = fields.Datetime.now() + relativedelta(days=5)
        order_needed_soon.with_context(clearance_internal_write=True).write(
            {"clearance_date": fields.Datetime.now()}
        )

        order_earlier_clearance = self._make_order(self.partner_b, 10)
        order_earlier_clearance.pick_scheduled_date = fields.Datetime.now() + relativedelta(days=60)
        order_earlier_clearance.with_context(clearance_internal_write=True).write(
            {"clearance_date": fields.Datetime.now() - timedelta(days=1)}
        )

        for order in (order_needed_soon, order_earlier_clearance):
            order.order_line.move_ids.invalidate_recordset()
            self.assertNotEqual(order.order_line.move_ids.state, "assigned")

        report = self.env["stock.forecasted_product_product"].with_context(warehouse=self.warehouse.id)
        data = report._get_report_data(product_ids=[self.product.id])
        order_ids_in_order = [
            l["document_out"]["id"] for l in data["lines"]
            if l.get("document_out") and l["document_out"].get("_name") == "sale.order"
        ]
        self.assertLess(
            order_ids_in_order.index(order_earlier_clearance.id),
            order_ids_in_order.index(order_needed_soon.id),
            "earlier clearance timestamp must sort first, even though it's needed later by delivery date",
        )

    def test_forecast_reserved_lines_sort_by_delivery_not_clearance_date(self):
        """Explicit product decision, the mirror image of the unreserved
        block's own rule: once stock is actually in hand, clearance
        priority has already done its job — deciding WHO holds it. From
        there, WHEN it's needed is what matters, so the reserved block
        sorts by delivery date, not clearance date."""
        self._set_stock(20)
        order_earlier_clearance = self._make_order(self.partner_a, 10)
        order_earlier_clearance.pick_scheduled_date = fields.Datetime.now() + relativedelta(days=60)
        # fields.Datetime.now() truncates to whole seconds, so two orders
        # confirmed within the same second can otherwise land on an
        # identical clearance_date — pin them explicitly apart instead of
        # relying on real-time confirmation timing.
        order_earlier_clearance.with_context(clearance_internal_write=True).write(
            {"clearance_date": fields.Datetime.now() - timedelta(days=1)}
        )

        order_needed_soon = self._make_order(self.partner_b, 10)
        order_needed_soon.pick_scheduled_date = fields.Datetime.now() + relativedelta(days=5)

        for order in (order_earlier_clearance, order_needed_soon):
            order.order_line.invalidate_recordset()
            self.assertEqual(order.order_line.move_ids.quantity, 10, "fully reserved")
        self.assertLess(order_earlier_clearance.clearance_date, order_needed_soon.clearance_date)

        report = self.env["stock.forecasted_product_product"].with_context(warehouse=self.warehouse.id)
        data = report._get_report_data(product_ids=[self.product.id])
        order_ids_in_order = [
            l["document_out"]["id"] for l in data["lines"]
            if l.get("document_out") and l["document_out"].get("_name") == "sale.order"
        ]
        self.assertLess(
            order_ids_in_order.index(order_needed_soon.id),
            order_ids_in_order.index(order_earlier_clearance.id),
            "once reserved, the line needed SOONER must sort first, despite its later clearance timestamp",
        )

    def test_forecast_split_line_remainder_stays_beneath_its_own_reserved_portion(self):
        """Even though the reserved block now sorts by delivery date, a
        split line's own still-unfulfilled remainder must stay pinned
        directly beneath its own reserved portion — never sorted away
        from it, regardless of what else is in the report."""
        self._set_stock(6)
        order = self._make_order(self.partner_a, 10)
        scheduled = fields.Datetime.now() + relativedelta(days=30)
        order.pick_scheduled_date = scheduled
        order.order_line.invalidate_recordset()
        self.assertEqual(order.order_line.move_ids.quantity, 6, "partially fulfilled")

        self._create_committed_po(10, scheduled - timedelta(days=14))
        self.env.invalidate_all()
        self.assertEqual(order.order_line.clearance_defer_reason, "Scheduled Future Stock")

        # An unrelated, fully reserved order for a different product,
        # needed LATER than this split order's own 30-day delivery date.
        # The reserved block sorts by delivery date and always sits
        # entirely before the unreserved block — so without the pinning
        # logic, this later-needed order would sort AFTER the split
        # line's reserved chunk within the reserved block, landing
        # between it and the remainder (which only appears once the
        # whole reserved block ends). A sooner-needed decoy would NOT
        # prove this — it sorts before the split's reserved chunk, never
        # between the two rows.
        other_product = self.env["product.product"].create({
            "name": "Split Adjacency Widget", "is_storable": True,
        })
        self._set_stock(10, product=other_product)
        later_order = self._make_order(self.partner_b, 10, product=other_product)
        later_order.pick_scheduled_date = fields.Datetime.now() + relativedelta(days=90)

        report = self.env["stock.forecasted_product_product"].with_context(warehouse=self.warehouse.id)
        data = report._get_report_data(product_ids=[self.product.id, other_product.id])
        order_ids_in_order = [
            l["document_out"]["id"] for l in data["lines"]
            if l.get("document_out") and l["document_out"].get("_name") == "sale.order"
        ]
        order_indexes = [i for i, oid in enumerate(order_ids_in_order) if oid == order.id]
        self.assertEqual(len(order_indexes), 2, "expected the reserved chunk and the waiting remainder")
        self.assertEqual(
            order_indexes[1], order_indexes[0] + 1,
            "the split line's remainder must sit directly beneath its own reserved portion",
        )

    def test_forecast_incoming_allocation_orders_by_clearance_priority_not_creation_order(self):
        """The forecast engine must rank by clearance priority, not by
        whichever order happened to be created (or scheduled) first —
        the same guarantee _reserve_by_clearance provides for CURRENT
        stock, now extended to a committed but not-yet-arrived shipment.
        A naive "whoever's first in the queryset" ordering would hand
        this to the order created first; clearance priority says
        otherwise."""
        self._set_stock(0)
        first_created = self._make_order(self.partner_a, 10)
        second_created = self._make_order(self.partner_b, 10)
        second_created.with_context(clearance_internal_write=True).write(
            {"clearance_date": fields.Datetime.now() - timedelta(days=1)}
        )
        self._create_committed_po(10, fields.Datetime.now() + timedelta(days=20))
        self.env.invalidate_all()

        allocation = self.env["sale.order"]._forecast_incoming_allocation(product_ids=[self.product.id])
        first_move = first_created.order_line.move_ids
        second_move = second_created.order_line.move_ids
        self.assertEqual(
            sum(e["qty"] for e in allocation.get(second_move.id, [])), 10,
            "the earlier-clearance order wins the shipment despite being created second",
        )
        self.assertEqual(allocation.get(first_move.id, []), [], "nothing left for the later-clearance order")

        second_created.order_line.invalidate_recordset()
        self.assertEqual(second_created.order_line.expected_incoming_qty, 10, "exposed the same way via the field")
        self.assertTrue(second_created.order_line.expected_incoming_fully_covers_remaining)

    def test_forecast_incoming_allocation_splits_across_multiple_shipments(self):
        """A demand line spanning more than one committed shipment gets
        an itemized breakdown, not just a single collapsed number — an
        external consumer needs to know it's coming in two pieces."""
        self._set_stock(0)
        order = self._make_order(self.partner_a, 15)
        self._create_committed_po(10, fields.Datetime.now() + timedelta(days=10))
        po2 = self._create_committed_po(10, fields.Datetime.now() + timedelta(days=20))
        po2_move = po2.picking_ids.move_ids
        self.env.invalidate_all()
        order.order_line.invalidate_recordset()

        self.assertEqual(order.order_line.expected_incoming_qty, 15)
        self.assertTrue(order.order_line.expected_incoming_fully_covers_remaining)
        breakdown = order.order_line.expected_incoming_breakdown
        self.assertEqual(len(breakdown), 2, "expected one entry per committed shipment drawn from")
        self.assertEqual(sum(e["qty"] for e in breakdown), 15)
        self.assertEqual(
            order.order_line.expected_incoming_date, po2_move.date,
            "the date shown is when the LAST needed shipment lands, not the first partial arrival",
        )

    def test_forecast_incoming_allocation_partial_coverage(self):
        """When even every committed shipment together falls short of the
        line's own demand, that must be visible, not silently rounded up
        to 'covered'."""
        self._set_stock(0)
        order = self._make_order(self.partner_a, 20)
        self._create_committed_po(8, fields.Datetime.now() + timedelta(days=10))
        self.env.invalidate_all()
        order.order_line.invalidate_recordset()

        self.assertEqual(order.order_line.expected_incoming_qty, 8)
        self.assertFalse(
            order.order_line.expected_incoming_fully_covers_remaining,
            "8 committed against a demand of 20 must not read as fully covered",
        )

    def test_forecast_incoming_allocation_respects_force_reserve_and_hard_lock_tiers(self):
        """The same tiering _reserve_by_clearance uses for real stock must
        hold for a forecast against a not-yet-arrived shipment too: a
        force-reserved line outranks an earlier-clearance but otherwise
        ordinary competitor, and a purely hard-locked no_invoice line
        (no genuine clearance, no force-reserve of its own) has no
        acquisition ability at all — same as it has none in the real
        queue."""
        self._set_stock(0)
        early_clearance = self._make_order(self.partner_a, 10)
        early_clearance.with_context(clearance_internal_write=True).write(
            {"clearance_date": fields.Datetime.now() - timedelta(days=5)}
        )
        force_reserved = self._make_admin_only_order(self.partner_b, 10)
        force_reserved.order_line.write({"is_force_reserved": True})
        hard_locked_no_invoice = self._make_admin_only_order(self.partner_c, 10)
        hard_locked_no_invoice.order_line.write({"is_reservation_hard_locked": True})

        self._create_committed_po(10, fields.Datetime.now() + timedelta(days=10))
        self.env.invalidate_all()

        allocation = self.env["sale.order"]._forecast_incoming_allocation(product_ids=[self.product.id])
        force_move = force_reserved.order_line.move_ids
        early_move = early_clearance.order_line.move_ids
        locked_move = hard_locked_no_invoice.order_line.move_ids

        self.assertEqual(
            sum(e["qty"] for e in allocation.get(force_move.id, [])), 10,
            "force-reserve outranks an earlier-clearance but non-overridden competitor",
        )
        self.assertEqual(
            allocation.get(early_move.id, []), [],
            "nothing left for the earlier-clearance line once force-reserve took it",
        )
        self.assertNotIn(
            locked_move.id, allocation,
            "a purely hard-locked no_invoice line has no acquisition ability at all, so it's never even considered",
        )

    def test_forecast_incoming_allocation_excludes_far_future_unless_overridden(self):
        """Mirrors test_far_future_orders_excluded_unless_overridden for
        the forecast side: an order scheduled more than 6 months out must
        not be shown as entitled to a committed shipment either, unless
        hard-locked or force-reserved."""
        self._set_stock(0)
        order = self._make_order(self.partner_a, 10)
        order.pick_scheduled_date = fields.Datetime.now() + relativedelta(months=8)
        self._create_committed_po(10, fields.Datetime.now() + timedelta(days=10))
        self.env.invalidate_all()

        allocation = self.env["sale.order"]._forecast_incoming_allocation(product_ids=[self.product.id])
        move = order.order_line.move_ids
        self.assertEqual(
            allocation.get(move.id, []), [],
            "a far-future order must not be forecast as covered, even by a committed shipment",
        )

        order.write({"is_reservation_hard_locked": True})
        self.env.invalidate_all()
        allocation = self.env["sale.order"]._forecast_incoming_allocation(product_ids=[self.product.id])
        self.assertEqual(
            sum(e["qty"] for e in allocation.get(move.id, [])), 10,
            "hard lock overrides the far-future exclusion in the forecast too",
        )

    def test_forecast_incoming_allocation_ignores_chained_ship_leg(self):
        """An order whose Pick has already completed (only its Ship leg
        still open) must never itself be forecast as claiming incoming
        stock — that demand was already satisfied by the Pick; counting
        the chained Ship move too would double-book the same units."""
        self._set_stock(10)
        order = self._make_order(self.partner_a, 10)
        pick = order.picking_ids.filtered(
            lambda p: p.picking_type_id == order.warehouse_id.pick_type_id
        )
        pick.button_validate()

        ship_move = order.order_line.move_ids.filtered(lambda m: m.state not in ("done", "cancel"))
        self.assertTrue(ship_move, "Ship leg should still be open")
        self.assertNotEqual(ship_move.picking_type_id, self.warehouse.pick_type_id)

        self._create_committed_po(10, fields.Datetime.now() + timedelta(days=10))
        self.env.invalidate_all()
        allocation = self.env["sale.order"]._forecast_incoming_allocation(product_ids=[self.product.id])
        self.assertNotIn(ship_move.id, allocation, "the Ship leg must never itself claim incoming stock")

    def test_forecast_incoming_allocation_agrees_with_scheduled_future_stock_release(self):
        """The forecast must predict exactly what _reserve_by_clearance
        will actually do once a promised shipment lands — reconstructs
        the same holder/early/force-reserve scenario as
        test_scheduled_future_stock_releases_to_earlier_scheduled_order_and_reclaims_ahead_of_force_reserve,
        but checks the forecast BEFORE the PO physically arrives."""
        self._set_stock(10)
        holder = self._make_order(self.partner_a, 10)
        holder_scheduled = fields.Datetime.now() + relativedelta(days=30)
        holder.pick_scheduled_date = holder_scheduled
        holder.order_line.invalidate_recordset()
        self.assertEqual(holder.order_line.move_ids.quantity, 10)

        po = self._create_committed_po(10, holder_scheduled - timedelta(days=14))
        self.env.invalidate_all()
        self.assertEqual(holder.order_line.clearance_defer_reason, "Scheduled Future Stock")

        early = self._make_order(self.partner_b, 10)
        early.pick_scheduled_date = fields.Datetime.now() + relativedelta(days=5)
        self.env["sale.order"]._reserve_by_clearance(product_ids=[self.product.id])
        holder.order_line.invalidate_recordset()
        early.order_line.invalidate_recordset()
        self.assertEqual(holder.order_line.move_ids.quantity, 0, "holder gave up its stock")

        early.order_line.write({"is_reservation_hard_locked": True})
        late_force = self._make_admin_only_order(self.partner_c, 10)
        late_force.order_line.write({"is_force_reserved": True})

        # Before the PO ever arrives: the forecast must already show the
        # released holder, not the later force-reserve, as entitled to it.
        allocation = self.env["sale.order"]._forecast_incoming_allocation(product_ids=[self.product.id])
        holder_move = holder.order_line.move_ids
        force_move = late_force.order_line.move_ids
        self.assertEqual(
            sum(e["qty"] for e in allocation.get(holder_move.id, [])), 10,
            "forecast must credit the released holder ahead of the later force-reserve",
        )
        self.assertEqual(allocation.get(force_move.id, []), [])

        # And once it actually lands, real reservation must agree with
        # what the forecast already predicted.
        po.picking_ids.button_validate()
        holder.order_line.invalidate_recordset()
        late_force.order_line.invalidate_recordset()
        self.assertEqual(
            holder.order_line.move_ids.filtered(lambda m: m.state not in ("done", "cancel")).quantity, 10,
        )
        self.assertEqual(late_force.order_line.move_ids.quantity, 0)

    def test_stock_forecasted_report_exposes_incoming_forecast_and_suppresses_misleading_flag(self):
        """The forecast report must surface this module's own
        clearance-priority-correct forecast, and flag that native's own
        Reserve link (if it would otherwise show one here) is driven by
        priority-blind ins-matching rather than this module's queue."""
        self._set_stock(0)
        order = self._make_order(self.partner_a, 10)
        po = self._create_committed_po(10, fields.Datetime.now() + timedelta(days=15))
        self.env.invalidate_all()
        order.order_line.invalidate_recordset()

        report = self.env["stock.forecasted_product_product"].with_context(warehouse=self.warehouse.id)
        data = report._get_report_data(product_ids=[self.product.id])
        lines_for_order = [
            l for l in data["lines"]
            if l.get("document_out") and l["document_out"].get("id") == order.id
        ]
        self.assertTrue(lines_for_order)
        line = lines_for_order[0]
        self.assertTrue(line.get("clearance_incoming_forecast"))
        self.assertEqual(
            line["clearance_incoming_forecast"]["qty"], order.order_line.expected_incoming_qty,
        )
        self.assertEqual(
            line["clearance_incoming_forecast"]["purchase_order_id"], po.id,
            "needed so the frontend can render a clickable link straight to the real PO",
        )
        self.assertTrue(
            line.get("clearance_reserve_is_misleading"),
            "an unreserved out-line must be flagged so the JS knows not to trust native's own Reserve link here",
        )

    def test_purchase_order_is_receipt_confirmed_requires_both_fields(self):
        """The receive date only counts as genuinely confirmed once BOTH
        the container reference and the port arrival date are on file —
        either alone is not enough."""
        po = self._create_committed_po(10, fields.Datetime.now() + timedelta(days=15), confirmed=False)
        self.assertFalse(po.is_receipt_confirmed, "neither field set yet")

        po.container_reference = "MSCU1234567"
        self.assertFalse(po.is_receipt_confirmed, "only the container reference is set")

        po.port_arrival_date = fields.Date.today()
        self.assertTrue(po.is_receipt_confirmed, "both fields are now set")

        po.container_reference = False
        self.assertFalse(po.is_receipt_confirmed, "clearing either field un-confirms it again")

    def test_confirming_receipt_pushes_transit_time_onto_date_planned_and_move_date(self):
        """Confirming a PO (container reference + port arrival date both
        on file) must push the port arrival date plus the fixed
        harbor-to-warehouse transit time (GOODS_TRANSIT_DAYS) onto the
        PO line's own date_planned AND its linked incoming move's own
        date — not just date_planned alone, since date_planned's own
        write() (purchase_stock) only ever updates the move's
        date_deadline, never its date, which is what this module's whole
        engine actually reads."""
        po = self._create_committed_po(10, fields.Datetime.now() + timedelta(days=1), confirmed=False)
        move = po.picking_ids.move_ids
        port_arrival_date = fields.Date.today() + timedelta(days=5)
        expected = fields.Datetime.to_datetime(port_arrival_date) + timedelta(days=GOODS_TRANSIT_DAYS)

        po.write({"container_reference": "MSCU2222222", "port_arrival_date": port_arrival_date})
        move.invalidate_recordset()

        self.assertEqual(po.order_line.date_planned, expected, "Expected Arrival must reflect the transit-adjusted date")
        self.assertEqual(move.date, expected, "the move's own date is what the engine actually reads")

    def test_editing_expected_arrival_by_hand_updates_the_move_date_too(self):
        """Bug found live: editing "Expected Arrival" directly (an
        unconfirmed PO — no separate transit-time sync involved at all)
        only ever updated the move's date_deadline via native's own
        write() (purchase_stock) — never its actual date, the field this
        module's whole engine reads. The forecast's Receipt column
        silently kept showing the ORIGINAL date forever, no matter how
        many times "Expected Arrival" was edited by hand."""
        po = self._create_committed_po(10, fields.Datetime.now() + timedelta(days=10), confirmed=False)
        move = po.picking_ids.move_ids
        new_date = fields.Datetime.now() + timedelta(days=25)

        po.order_line.write({"date_planned": new_date})
        move.invalidate_recordset()

        self.assertEqual(move.date, new_date, "the move's own date must follow a manual Expected Arrival edit too")

    def test_changing_committed_po_date_auto_reruns_the_queue(self):
        """Bug motivation: a promised future shipment's own date can move
        (a rescheduled PO, or purchase_order.py's own transit-time sync)
        after a holder has already released stock under "Scheduled
        Future Stock" — the queue must re-evaluate immediately, not wait
        for some unrelated later event to eventually touch the same
        product. Deliberately never calls _reserve_by_clearance
        explicitly — only stock_move.py's own write() hook should."""
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
        self.assertEqual(holder.order_line.move_ids.quantity, 0, "holder gave up its stock")
        self.assertTrue(holder.order_line.is_scheduled_future_stock_release)

        # The promised shipment's port arrival slips to AFTER the
        # holder's own scheduled date — no margin left at all. Only the
        # PO write happens here, no explicit _reserve_by_clearance call.
        po.write({"port_arrival_date": holder_scheduled.date() + timedelta(days=1)})
        self.env.invalidate_all()

        self.assertFalse(
            holder.order_line.is_scheduled_future_stock_release,
            "the move's own date changing must have re-run the queue immediately, "
            "via stock_move.py's write() hook, with no explicit trigger",
        )

    def test_forecast_surfaces_purchase_order_confirmed_status(self):
        """A confirmed PO's status must flow all the way through: the
        allocation engine, the plugin-facing breakdown on the sale order
        line, and the forecast report's own line dict — so the frontend
        badge and any external consumer agree with each other."""
        self._set_stock(0)
        order = self._make_order(self.partner_a, 10)
        po = self._create_committed_po(10, fields.Datetime.now() + timedelta(days=15))
        self.env.invalidate_all()
        order.order_line.invalidate_recordset()

        self.assertTrue(po.is_receipt_confirmed)
        breakdown = order.order_line.expected_incoming_breakdown
        self.assertTrue(breakdown)
        self.assertTrue(
            breakdown[0]["purchase_order_confirmed"],
            "the plugin-facing breakdown must carry the PO's confirmed status",
        )

        report = self.env["stock.forecasted_product_product"].with_context(warehouse=self.warehouse.id)
        data = report._get_report_data(product_ids=[self.product.id])
        lines_for_order = [
            l for l in data["lines"]
            if l.get("document_out") and l["document_out"].get("id") == order.id
        ]
        self.assertTrue(lines_for_order)
        self.assertTrue(
            lines_for_order[0]["clearance_incoming_forecast"]["confirmed"],
            "the forecast report's own line must reflect the same confirmed status",
        )

    def _make_draft_order(self, partner, qty, product=None, commitment_date=None):
        vals = {
            "partner_id": partner.id,
            "order_line": [(0, 0, {
                "product_id": (product or self.product).id,
                "product_uom_qty": qty,
            })],
        }
        if commitment_date is not None:
            vals["commitment_date"] = commitment_date
        return self.env["sale.order"].create(vals)

    def test_clearance_availability_draft_line_sees_only_real_leftover_on_hand(self):
        """A draft (unconfirmed) line's simulated availability must
        respect stock a higher-priority REAL order already holds — it
        only ever sees genuine leftover, never the gross on-hand total."""
        self._set_stock(10)
        real_order = self._make_order(self.partner_a, 6)
        real_order.order_line.invalidate_recordset()
        self.assertEqual(real_order.order_line.move_ids.quantity, 6, "real order holds its 6 first")

        draft = self._make_draft_order(self.partner_b, 10)
        self.env.invalidate_all()
        line = draft.order_line

        self.assertEqual(line.clearance_availability_qty, 4, "only the genuine leftover, not the gross 10 on hand")
        self.assertFalse(line.clearance_availability_fully_covered)
        self.assertEqual(line.clearance_availability_status, "short")

    def test_clearance_availability_draft_line_forecasts_future_po_coverage(self):
        """A draft line's remaining need, once on-hand is exhausted, must
        be forecast against committed future incoming POs — same
        priority-aware allocation the real engine would use once
        confirmed."""
        self._set_stock(0)
        far_future = fields.Datetime.now() + relativedelta(days=60)
        draft = self._make_draft_order(self.partner_a, 10, commitment_date=far_future)
        po = self._create_committed_po(10, fields.Datetime.now() + timedelta(days=20))
        self.env.invalidate_all()
        line = draft.order_line

        self.assertEqual(line.clearance_availability_qty, 10)
        self.assertTrue(line.clearance_availability_fully_covered)
        self.assertEqual(line.clearance_availability_date, po.picking_ids.move_ids.date)
        self.assertEqual(line.clearance_availability_status, "available")
        self.assertIn(po.name, line.clearance_availability_source)
        self.assertEqual(
            line.clearance_reserved_qty, "0",
            "must reflect what's in stock NOW, not the full forecast including the future PO",
        )
        self.assertEqual(len(line.clearance_availability_breakdown), 1)
        self.assertEqual(
            line.clearance_availability_breakdown[0]["purchase_order_id"], po.id,
            "the itemized breakdown must carry the PO's real id so the tooltip can link to it",
        )
        self.assertEqual(line.clearance_availability_breakdown[0]["purchase_order_name"], po.name)

    def test_clearance_availability_draft_line_short_with_nothing_committed(self):
        """Nothing on hand and nothing committed anywhere — the draft
        line must show as genuinely short, not silently zero-but-fine."""
        self._set_stock(0)
        draft = self._make_draft_order(self.partner_a, 10)
        self.env.invalidate_all()
        line = draft.order_line

        self.assertEqual(line.clearance_availability_qty, 0.0)
        self.assertFalse(line.clearance_availability_fully_covered)
        self.assertEqual(line.clearance_availability_status, "short")

    def test_clearance_availability_confirmed_order_reflects_real_state(self):
        """Once confirmed, the same badge must reflect the real
        reservation state directly — no simulation involved."""
        self._set_stock(10)
        order = self._make_order(self.partner_a, 10)
        order.order_line.invalidate_recordset()
        line = order.order_line

        self.assertEqual(line.clearance_availability_qty, 10)
        self.assertTrue(line.clearance_availability_fully_covered)
        self.assertEqual(line.clearance_availability_status, "available")

    def test_clearance_availability_late_when_covered_after_commitment_date(self):
        """Fully covered eventually by a committed PO, but only after the
        order's own commitment date — must be flagged late, not shown as
        plainly available."""
        self._set_stock(0)
        soon = fields.Datetime.now() + relativedelta(days=5)
        draft = self._make_draft_order(self.partner_a, 10, commitment_date=soon)
        self._create_committed_po(10, fields.Datetime.now() + timedelta(days=20))
        self.env.invalidate_all()
        line = draft.order_line

        self.assertTrue(line.clearance_availability_fully_covered)
        self.assertTrue(line.clearance_availability_late)
        self.assertEqual(line.clearance_availability_status, "late")


@tagged("post_install", "-at_install")
class TestClearanceBinStock(TransactionCase):
    """clearance.bin.stock is deliberately inert as far as reservation is
    concerned — it exists purely so a person can record where a product's
    overstock physically sits, decoupled entirely from stock.quant. These
    tests cover only that model's own behavior; they never touch
    _reserve_by_clearance, by design."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.product = cls.env["product.product"].create({
            "name": "Test Bin Stock Widget",
            "is_storable": True,
        })
        cls.warehouse = cls.env["stock.warehouse"].search([], limit=1)
        cls.bin_a = cls.env["stock.location"].create({
            "name": "Test Bin A",
            "location_id": cls.warehouse.view_location_id.id,
            "usage": "internal",
        })

    def test_add_quantity_creates_and_increments(self):
        record = self.env["clearance.bin.stock"]._get_or_create(self.product, self.bin_a)
        self.assertEqual(record.quantity, 0)
        record.add_quantity(20, note="fresh pallet")
        self.assertEqual(record.quantity, 20)
        record.add_quantity(5)
        self.assertEqual(record.quantity, 25)

    def test_get_or_create_reuses_existing_record(self):
        first = self.env["clearance.bin.stock"]._get_or_create(self.product, self.bin_a)
        first.add_quantity(10)
        second = self.env["clearance.bin.stock"]._get_or_create(self.product, self.bin_a)
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.quantity, 10)

    def test_remove_quantity_decrements(self):
        record = self.env["clearance.bin.stock"]._get_or_create(self.product, self.bin_a)
        record.add_quantity(20)
        record.remove_quantity(8, note="carried to Main")
        self.assertEqual(record.quantity, 12)

    def test_remove_quantity_blocks_going_negative(self):
        record = self.env["clearance.bin.stock"]._get_or_create(self.product, self.bin_a)
        record.add_quantity(5)
        with self.assertRaises(UserError):
            record.remove_quantity(10)
        self.assertEqual(record.quantity, 5, "a blocked removal must not partially apply")

    def test_add_and_remove_reject_non_positive_quantities(self):
        record = self.env["clearance.bin.stock"]._get_or_create(self.product, self.bin_a)
        with self.assertRaises(UserError):
            record.add_quantity(0)
        with self.assertRaises(UserError):
            record.remove_quantity(-1)

    def test_place_pallet_offers_any_buffer_bin_regardless_of_occupancy(self):
        """The Place Pallet wizard's own location domain offers any bin
        under Buffer Zone or Main — empty, holding this same product, or
        holding a different one. Explicit product decision: some
        locations deliberately hold multiple different products, so
        occupancy is never a reason to hide a bin — only a bin outside
        Buffer Zone and Main is excluded."""
        buffer_zone = self.env["stock.location"].search([("name", "=", "Buffer Zone")], limit=1)
        if not buffer_zone:
            self.skipTest("No 'Buffer Zone' location configured in this database")
        empty_bin = self.env["stock.location"].create({
            "name": "Test Empty Buffer Bin", "location_id": buffer_zone.id, "usage": "internal",
        })
        same_product_bin = self.env["stock.location"].create({
            "name": "Test Same-Product Buffer Bin", "location_id": buffer_zone.id, "usage": "internal",
        })
        self.env["clearance.bin.stock"]._get_or_create(self.product, same_product_bin).add_quantity(3)
        other_product_bin = self.env["stock.location"].create({
            "name": "Test Other-Product Buffer Bin", "location_id": buffer_zone.id, "usage": "internal",
        })
        other_product = self.env["product.product"].create({
            "name": "Test Bin Stock Widget — Other", "is_storable": True,
        })
        self.env["clearance.bin.stock"]._get_or_create(other_product, other_product_bin).add_quantity(5)

        wizard = self.env["clearance.bin.stock.wizard"].create({
            "mode": "add", "product_id": self.product.id, "location_id": empty_bin.id, "quantity": 1,
        })
        available_ids = wizard.available_location_ids.ids
        self.assertIn(empty_bin.id, available_ids)
        self.assertIn(
            same_product_bin.id, available_ids,
            "topping up a bin that already holds more of the SAME product must be allowed",
        )
        self.assertIn(
            other_product_bin.id, available_ids,
            "some locations deliberately hold multiple products — a bin holding a "
            "different product must still be offered",
        )
        self.assertNotIn(
            self.bin_a.id, available_ids,
            "a bin outside Buffer Zone and Main must never be offered for placing a pallet",
        )

    def test_place_pallet_includes_main_location(self):
        """Receiving straight into Main (or topping it up) must also be
        possible from Place Pallet — not just Buffer bins."""
        pick_rule = self.env["stock.rule"].search(
            [("picking_type_id", "=", self.warehouse.pick_type_id.id)], limit=1
        )
        main_zone = pick_rule.location_src_id or self.warehouse.lot_stock_id
        main_bin = self.env["stock.location"].create({
            "name": "Test Main Bin", "location_id": main_zone.id, "usage": "internal",
        })
        wizard = self.env["clearance.bin.stock.wizard"].create({
            "mode": "add", "product_id": self.product.id, "location_id": main_bin.id, "quantity": 1,
        })
        self.assertIn(
            main_bin.id, wizard.available_location_ids.ids,
            "an empty bin under Main must be offered when placing a pallet",
        )

    def test_remove_pallet_is_not_restricted_to_buffer_zone(self):
        """Remove mode is the opposite situation — you're taking stock
        FROM wherever it already exists, which could be any bin — so it
        must not inherit Place Pallet's empty-Buffer-only restriction."""
        wizard = self.env["clearance.bin.stock.wizard"].create({
            "mode": "remove", "product_id": self.product.id, "location_id": self.bin_a.id, "quantity": 1,
        })
        self.assertIn(
            self.bin_a.id, wizard.available_location_ids.ids,
            "remove mode must not be restricted to Buffer Zone",
        )

    def test_changes_never_touch_real_stock_or_reservation(self):
        """The whole point of this model: logging bin stock must never
        create, modify, or otherwise interact with any stock.quant, and
        must never trigger the reservation queue."""
        before_quants = self.env["stock.quant"].search_count([
            ("product_id", "=", self.product.id)
        ])
        record = self.env["clearance.bin.stock"]._get_or_create(self.product, self.bin_a)
        record.add_quantity(50)
        record.remove_quantity(20)
        after_quants = self.env["stock.quant"].search_count([
            ("product_id", "=", self.product.id)
        ])
        self.assertEqual(before_quants, after_quants, "must never create or touch a real stock.quant")
        self.assertEqual(self.product.qty_available, 0, "real on-hand must be completely unaffected")

    def test_bin_stock_total_matches_when_logged_correctly(self):
        bin_b = self.env["stock.location"].create({
            "name": "Test Bin B",
            "location_id": self.warehouse.view_location_id.id,
            "usage": "internal",
        })
        self.env["clearance.bin.stock"]._get_or_create(self.product, self.bin_a).add_quantity(10)
        self.env["clearance.bin.stock"]._get_or_create(self.product, bin_b).add_quantity(15)
        self.product.invalidate_recordset()
        self.assertEqual(self.product.bin_stock_total, 25)
        self.assertEqual(self.product.bin_stock_count, 2)

    def test_bin_stock_discrepancy_flags_mismatch_against_real_stock(self):
        pick_rule = self.env["stock.rule"].search(
            [("picking_type_id", "=", self.warehouse.pick_type_id.id)], limit=1
        )
        pick_source_location = pick_rule.location_src_id or self.warehouse.lot_stock_id
        quant = self.env["stock.quant"].create({
            "product_id": self.product.id,
            "location_id": pick_source_location.id,
        })
        quant.with_context(inventory_mode=True).write({"inventory_quantity": 30})
        quant.action_apply_inventory()

        self.env["clearance.bin.stock"]._get_or_create(self.product, self.bin_a).add_quantity(30)
        self.product.invalidate_recordset()
        self.assertEqual(
            self.product.bin_stock_discrepancy, 0,
            "tracked total matches real on-hand — logged correctly",
        )
        self.assertEqual(
            self.product.bin_stock_reference_qty, 30,
            "the actual figure being compared against must be visible on its own, "
            "never left implicit — comparing against Odoo's native On Hand field "
            "instead (which also includes Output) would be misleading",
        )

        self.env["clearance.bin.stock"]._get_or_create(self.product, self.bin_a).add_quantity(5)
        self.product.invalidate_recordset()
        self.assertEqual(
            self.product.bin_stock_discrepancy, 5,
            "tracked total now exceeds real on-hand — should surface as a mismatch to investigate",
        )

    def test_discrepancies_report_only_lists_mismatched_tracked_products(self):
        """The report scopes to products that actually have a
        clearance.bin.stock record at all, and within those, only ones
        whose tracked total doesn't match real stock — never every
        product in the database, and never a product that's fine."""
        mismatched_product = self.product

        matching_product = self.env["product.product"].create({
            "name": "Test Bin Stock Widget — Matching", "is_storable": True,
        })
        matching_bin = self.env["stock.location"].create({
            "name": "Test Bin Matching", "location_id": self.warehouse.view_location_id.id,
            "usage": "internal",
        })
        self.env["clearance.bin.stock"]._get_or_create(matching_product, matching_bin).add_quantity(10)
        # Real stock must also be 10 for this product to genuinely have
        # zero discrepancy — without this, tracked (10) vs real (0)
        # would itself be a mismatch, defeating the point of this fixture.
        pick_rule = self.env["stock.rule"].search(
            [("picking_type_id", "=", self.warehouse.pick_type_id.id)], limit=1
        )
        main_location = pick_rule.location_src_id or self.warehouse.lot_stock_id
        matching_quant = self.env["stock.quant"].create({
            "product_id": matching_product.id, "location_id": main_location.id,
        })
        matching_quant.with_context(inventory_mode=True).write({"inventory_quantity": 10})
        matching_quant.action_apply_inventory()

        untracked_product = self.env["product.product"].create({
            "name": "Test Bin Stock Widget — Untracked", "is_storable": True,
        })

        self.env["clearance.bin.stock"]._get_or_create(mismatched_product, self.bin_a).add_quantity(10)

        action = self.env["product.product"].action_view_bin_stock_discrepancies()
        # Extract the ('id', 'in', [...]) domain term directly.
        result_ids = next(term[2] for term in action["domain"] if term[0] == "id")

        self.assertIn(mismatched_product.id, result_ids)
        self.assertNotIn(matching_product.id, result_ids, "a product with no discrepancy must not appear")
        self.assertNotIn(untracked_product.id, result_ids, "a product with no bin stock at all must not appear")

    def test_reconcile_fix_bin_corrects_tracked_count(self):
        """Resolving via 'fix_bin' treats the tracker as wrong — it
        corrects the chosen bin's tracked quantity to bring the total
        back in line with real stock, touching no real stock.quant."""
        warehouse = self.warehouse
        pick_rule = self.env["stock.rule"].search(
            [("picking_type_id", "=", warehouse.pick_type_id.id)], limit=1
        )
        main_location = pick_rule.location_src_id or warehouse.lot_stock_id
        real_quant = self.env["stock.quant"].create({
            "product_id": self.product.id, "location_id": main_location.id,
        })
        real_quant.with_context(inventory_mode=True).write({"inventory_quantity": 20})
        real_quant.action_apply_inventory()

        bin_record = self.env["clearance.bin.stock"]._get_or_create(self.product, self.bin_a)
        bin_record.add_quantity(30)  # tracker over-counts by 10
        self.product.invalidate_recordset()
        self.assertEqual(self.product.bin_stock_discrepancy, 10)

        wizard = self.env["clearance.bin.stock.reconcile.wizard"].create({
            "product_id": self.product.id,
            "resolution": "fix_bin",
            "location_id": self.bin_a.id,
        })
        wizard.action_confirm()

        self.assertEqual(bin_record.quantity, 20, "the excess 10 should be removed from the chosen bin")
        self.product.invalidate_recordset()
        self.assertEqual(self.product.bin_stock_discrepancy, 0)
        real_quant.invalidate_recordset()
        self.assertEqual(real_quant.quantity, 20, "fix_bin must never touch real stock")

    def test_reconcile_fix_stock_adjusts_real_stock(self):
        """Resolving via 'fix_stock' treats the tracker as right — it
        creates a real inventory adjustment on Main instead, leaving
        every clearance.bin.stock record untouched."""
        warehouse = self.warehouse
        pick_rule = self.env["stock.rule"].search(
            [("picking_type_id", "=", warehouse.pick_type_id.id)], limit=1
        )
        main_location = pick_rule.location_src_id or warehouse.lot_stock_id
        real_quant = self.env["stock.quant"].create({
            "product_id": self.product.id, "location_id": main_location.id,
        })
        real_quant.with_context(inventory_mode=True).write({"inventory_quantity": 20})
        real_quant.action_apply_inventory()

        bin_record = self.env["clearance.bin.stock"]._get_or_create(self.product, self.bin_a)
        bin_record.add_quantity(30)  # tracker says 30, real stock says 20
        self.product.invalidate_recordset()
        self.assertEqual(self.product.bin_stock_discrepancy, 10)

        wizard = self.env["clearance.bin.stock.reconcile.wizard"].create({
            "product_id": self.product.id,
            "resolution": "fix_stock",
        })
        wizard.action_confirm()

        real_quant.invalidate_recordset()
        self.assertEqual(real_quant.quantity, 30, "real stock should be raised to match the tracker")
        self.assertEqual(bin_record.quantity, 30, "fix_stock must never touch the tracker")
        self.product.invalidate_recordset()
        self.assertEqual(self.product.bin_stock_discrepancy, 0)

    def test_reconcile_fix_stock_lands_at_products_existing_picking_sub_location(self):
        """Found via live data once already for auto-replenishment, and
        found again here: an inventory adjustment from fix_stock must
        land at the product's own dedicated picking sub-location, never
        the flat parent Pick-route location, when one already exists."""
        pick_rule = self.env["stock.rule"].search(
            [("picking_type_id", "=", self.warehouse.pick_type_id.id)], limit=1
        )
        picking_zone = pick_rule.location_src_id or self.warehouse.lot_stock_id
        product_spot = self.env["stock.location"].create({
            "name": "Test Product Spot", "location_id": picking_zone.id, "usage": "internal",
        })
        real_quant = self.env["stock.quant"].create({
            "product_id": self.product.id, "location_id": product_spot.id,
        })
        real_quant.with_context(inventory_mode=True).write({"inventory_quantity": 20})
        real_quant.action_apply_inventory()

        self.env["clearance.bin.stock"]._get_or_create(self.product, self.bin_a).add_quantity(30)
        self.product.invalidate_recordset()
        self.assertEqual(self.product.bin_stock_discrepancy, 10)

        wizard = self.env["clearance.bin.stock.reconcile.wizard"].create({
            "product_id": self.product.id,
            "resolution": "fix_stock",
        })
        wizard.action_confirm()

        real_quant.invalidate_recordset()
        self.assertEqual(
            real_quant.quantity, 30,
            "must land at the product's own dedicated picking spot, never the flat parent zone",
        )
        flat_quant = self.env["stock.quant"].search([
            ("product_id", "=", self.product.id), ("location_id", "=", picking_zone.id),
        ])
        self.assertFalse(flat_quant, "must never create a stray quant at the flat parent location")

    def test_reconcile_fix_bin_requires_location(self):
        self.env["clearance.bin.stock"]._get_or_create(self.product, self.bin_a).add_quantity(10)
        wizard = self.env["clearance.bin.stock.reconcile.wizard"].create({
            "product_id": self.product.id,
            "resolution": "fix_bin",
        })
        with self.assertRaises(UserError):
            wizard.action_confirm()

    def test_reconcile_raises_when_nothing_to_resolve(self):
        wizard = self.env["clearance.bin.stock.reconcile.wizard"].create({
            "product_id": self.product.id,
            "resolution": "fix_bin",
            "location_id": self.bin_a.id,
        })
        with self.assertRaises(UserError):
            wizard.action_confirm()
