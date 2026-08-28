from datetime import datetime, timedelta

from dateutil.relativedelta import relativedelta

from odoo import models, fields, api
from odoo.exceptions import AccessError
from odoo.tools import float_compare

from .sale_order import (
    FAR_FUTURE_MONTHS,
    SCHEDULED_FUTURE_STOCK_MATCH_BUFFER_DAYS,
    SCHEDULED_FUTURE_STOCK_RELEASE_BUFFER_DAYS,
    SCHEDULED_FUTURE_STOCK_RELEASE_BUFFER_DAYS_UNCONFIRMED,
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
             "any process, including manual overrides. Grants no acquisition "
             "power of its own — an unpaid, non-force-reserved line stays "
             "unpaid and non-force-reserved for queue purposes, and will "
             "never win new stock through the clearance queue on the strength "
             "of this lock alone.",
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

    # The queryable, always-fresh answer to "what's this line's forecasted
    # coverage from committed future incoming POs, in clearance-priority
    # order" — see sale.order._forecast_incoming_allocation. Never stored,
    # same "recomputed on every read" pattern as product.py's
    # bin_stock_total: this is a live judgment against the whole queue's
    # current state, not something dependency-trackable off this record's
    # own fields alone.
    expected_incoming_qty = fields.Float(
        compute="_compute_expected_incoming", digits="Product Unit of Measure",
        help="How much of this line's currently-open demand is covered by "
             "committed future incoming shipments, allocated in the exact "
             "same clearance-priority order real reservation uses. Not a "
             "reservation — nothing is held until the shipment actually "
             "arrives.",
    )
    # The LATEST shipment date needed to reach expected_incoming_qty — i.e.
    # when this line's forecasted (not yet real) coverage is complete, not
    # the first partial arrival.
    expected_incoming_date = fields.Datetime(compute="_compute_expected_incoming")
    expected_incoming_source = fields.Char(compute="_compute_expected_incoming")
    expected_incoming_fully_covers_remaining = fields.Boolean(compute="_compute_expected_incoming")
    # Per-shipment breakdown ({"qty", "expected_date", "purchase_order_id",
    # "purchase_order_name", "purchase_order_confirmed"}, one per committed
    # PO this line draws from) — the 4 fields above summarize this for the
    # common case; an external consumer that needs the full split reads
    # this instead.
    expected_incoming_breakdown = fields.Json(compute="_compute_expected_incoming")

    @api.depends()
    def _compute_expected_incoming(self):
        open_lines = self.filtered(
            lambda l: l.move_ids.filtered(lambda m: m.state not in ("done", "cancel", "assigned"))
        )
        # One call for the whole batch, never one per line — the ORM
        # already prefetches this compute across a recordset read together
        # (e.g. the forecast report's own outs batch); an external RPC
        # consumer should read a batch of ids the same way, not loop
        # read() one id at a time, or this degrades to one
        # _forecast_incoming_allocation call per line.
        allocation = (
            self.env["sale.order"]._forecast_incoming_allocation(product_ids=open_lines.product_id.ids)
            if open_lines else {}
        )
        for line in self:
            moves = line.move_ids.filtered(lambda m: m.state not in ("done", "cancel"))
            entries = [e for m in moves for e in allocation.get(m.id, [])]
            if not entries:
                line.update({
                    "expected_incoming_qty": 0.0,
                    "expected_incoming_date": False,
                    "expected_incoming_source": False,
                    "expected_incoming_fully_covers_remaining": False,
                    "expected_incoming_breakdown": [],
                })
                continue
            entries.sort(key=lambda e: e["expected_date"])
            total = sum(e["qty"] for e in entries)
            remaining = sum(m.product_qty - m.quantity for m in moves)
            rounding = line.product_uom.rounding or 0.01
            line.update({
                "expected_incoming_qty": total,
                "expected_incoming_date": entries[-1]["expected_date"],
                "expected_incoming_source": entries[-1]["purchase_order_name"],
                "expected_incoming_fully_covers_remaining": (
                    float_compare(total, remaining, precision_rounding=rounding) >= 0
                ),
                "expected_incoming_breakdown": [
                    {
                        "qty": e["qty"],
                        "expected_date": fields.Datetime.to_string(e["expected_date"]),
                        "purchase_order_id": e["purchase_order_id"],
                        "purchase_order_name": e["purchase_order_name"],
                        "purchase_order_confirmed": e["purchase_order_confirmed"],
                    }
                    for e in entries
                ],
            })

    # Queue-aware "will this actually be available by the delivery date"
    # answer, shown while a quotation is still being built (draft/sent —
    # no stock.move exists yet to ask _forecast_incoming_allocation
    # about) as well as after confirmation. Draft/sent: a live simulation
    # of joining the real clearance-priority queue right now
    # (sale.order._simulate_clearance_availability). Confirmed: the real
    # reservation/forecast state already computed elsewhere on this
    # model (expected_incoming_qty et al.) — no simulation needed, real
    # moves already tell the truth. Same "live judgment, not fully
    # dependency-trackable" caveat as expected_incoming_qty above: full
    # correctness depends on every OTHER order's current state too,
    # which @api.depends can't express — a caller doing a bulk mutation
    # should invalidate before re-reading, same convention already used
    # throughout this module's own test suite.
    clearance_availability_qty = fields.Float(
        compute="_compute_clearance_availability", digits="Product Unit of Measure",
        help="How much of this line's own demand would actually be "
             "available by its target delivery date — a live queue-aware "
             "simulation before confirmation, the real reservation/"
             "forecast state after.",
    )
    clearance_availability_fully_covered = fields.Boolean(compute="_compute_clearance_availability")
    # The date by which the FULL requested quantity becomes available —
    # False if already fully available right now, or if nothing
    # currently committed would ever fully cover it.
    clearance_availability_date = fields.Datetime(compute="_compute_clearance_availability")
    # Fully covered eventually, but not by the order's own target
    # delivery date (commitment_date, falling back to the native
    # lead-time estimate when unset — the same fallback native's own
    # _compute_qty_at_date already uses).
    clearance_availability_late = fields.Boolean(compute="_compute_clearance_availability")
    clearance_availability_source = fields.Char(compute="_compute_clearance_availability")
    # Same information as clearance_availability_source, but itemized
    # ({"prefix", "purchase_order_id", "purchase_order_name", "suffix"}
    # per chunk) rather than pre-flattened into one sentence — lets the
    # hover popover render the PO name as an actual clickable link
    # rather than inert text buried inside a string.
    clearance_availability_breakdown = fields.Json(compute="_compute_clearance_availability")
    # Summarizes the 3 fields above into one value a plain (non-QWeb)
    # form/list view can bind a widget="badge" + decoration-* to — the
    # same idiom sale_stock's own delivery_status field already uses,
    # rather than t-attf-class (a QWeb-only mechanic, not available on
    # this view).
    clearance_availability_status = fields.Selection(
        [("available", "Available"), ("late", "Late"), ("short", "Short")],
        compute="_compute_clearance_availability",
    )
    # How much of this line's own demand is ACTUALLY in stock/held right
    # now — deliberately NOT the full forecast (that's what
    # clearance_availability_qty/_source already cover, via the hover
    # tooltip on the icon next to this field). Shown as its own "Reserved"
    # field right next to Quantity, plain colored text — no badge.
    clearance_reserved_qty = fields.Char(
        string="Reserved", compute="_compute_clearance_availability",
    )

    def _clearance_format_qty(self, qty):
        return f"{qty:g}"

    @api.depends(
        "product_id", "product_uom_qty", "order_id.state", "order_id.warehouse_id",
        "order_id.commitment_date", "is_force_reserved", "is_reservation_hard_locked",
        "move_ids.state",
    )
    def _compute_clearance_availability(self):
        draft_lines = self.filtered(
            lambda l: l.order_id.state in ("draft", "sent") and l.product_id and l.product_uom_qty
        )
        # One call for the whole batch, same batching discipline as
        # _compute_expected_incoming above.
        simulation = (
            self.env["sale.order"]._simulate_clearance_availability(draft_lines)
            if draft_lines else {}
        )
        empty = {
            "clearance_availability_qty": 0.0,
            "clearance_availability_fully_covered": False,
            "clearance_availability_date": False,
            "clearance_availability_late": False,
            "clearance_availability_source": False,
            "clearance_availability_status": False,
            "clearance_availability_breakdown": [],
            "clearance_reserved_qty": False,
        }
        for line in self:
            if not line.product_id or not line.product_uom_qty:
                line.update(empty)
                continue
            order = line.order_id
            target_date = order.commitment_date or line._expected_date()
            rounding = line.product_uom.rounding or 0.01

            if order.state in ("draft", "sent"):
                sim = simulation.get(line.id)
                if not sim:
                    line.update(empty)
                    continue
                fully_covered = float_compare(
                    sim["total_qty"], line.product_uom_qty, precision_rounding=rounding
                ) >= 0
                covering_date = max((c["expected_date"] for c in sim["chunks"]), default=False) or False
                breakdown = []
                if sim["qty_now"] > 0:
                    breakdown.append({
                        "prefix": f"{self._clearance_format_qty(sim['qty_now'])} on hand now",
                        "purchase_order_id": False,
                        "purchase_order_name": False,
                        "suffix": "",
                    })
                for c in sim["chunks"]:
                    breakdown.append({
                        "prefix": f"{self._clearance_format_qty(c['qty'])} via ",
                        "purchase_order_id": c["purchase_order_id"],
                        "purchase_order_name": c["purchase_order_name"],
                        "suffix": f" by {fields.Date.to_string(c['expected_date'])}",
                    })
                source_parts = [
                    chunk["prefix"] + (chunk["purchase_order_name"] or "") + chunk["suffix"]
                    for chunk in breakdown
                ]
                if not breakdown:
                    breakdown = [{
                        "prefix": "Not available", "purchase_order_id": False,
                        "purchase_order_name": False, "suffix": "",
                    }]
                late = bool(
                    fully_covered and covering_date and target_date and covering_date > target_date
                )
                line.update({
                    "clearance_availability_qty": sim["total_qty"],
                    "clearance_availability_fully_covered": fully_covered,
                    "clearance_availability_date": covering_date,
                    "clearance_availability_late": late,
                    "clearance_availability_source": ", ".join(source_parts) or "Not available",
                    "clearance_availability_status": (
                        "short" if not fully_covered else "late" if late else "available"
                    ),
                    "clearance_availability_breakdown": breakdown,
                    # Deliberately qty_now, not total_qty — "how much is
                    # in stock for this right now", not the full forecast
                    # (on hand + future PO); the tooltip
                    # (clearance_availability_source) already narrates
                    # the full forecast breakdown.
                    "clearance_reserved_qty": self._clearance_format_qty(sim["qty_now"]),
                })
            else:
                # Confirmed: real moves already tell the truth — reuse
                # expected_incoming_qty/_date/_source (computed above)
                # for whatever's still open, plus whatever's already
                # actually held, rather than re-simulating.
                moves = line.move_ids.filtered(lambda m: m.state != "cancel")
                if not moves:
                    line.update(empty)
                    continue
                demand = sum(moves.mapped("product_uom_qty"))
                held_qty = sum(moves.mapped("quantity"))
                already_full = float_compare(held_qty, demand, precision_rounding=rounding) >= 0
                total_qty = held_qty + (0.0 if already_full else line.expected_incoming_qty)
                fully_covered = float_compare(total_qty, demand, precision_rounding=rounding) >= 0
                covering_date = False if already_full else line.expected_incoming_date
                if already_full:
                    source = "Available now"
                    breakdown = [{
                        "prefix": "Available now", "purchase_order_id": False,
                        "purchase_order_name": False, "suffix": "",
                    }]
                elif line.expected_incoming_source:
                    incoming_po_id = (
                        line.expected_incoming_breakdown[-1]["purchase_order_id"]
                        if line.expected_incoming_breakdown else False
                    )
                    source = (
                        f"{self._clearance_format_qty(held_qty)} reserved now, "
                        f"{self._clearance_format_qty(line.expected_incoming_qty)} more via "
                        f"{line.expected_incoming_source} by {fields.Date.to_string(line.expected_incoming_date)}"
                    )
                    breakdown = [{
                        "prefix": f"{self._clearance_format_qty(held_qty)} reserved now, "
                                  f"{self._clearance_format_qty(line.expected_incoming_qty)} more via ",
                        "purchase_order_id": incoming_po_id,
                        "purchase_order_name": line.expected_incoming_source,
                        "suffix": f" by {fields.Date.to_string(line.expected_incoming_date)}",
                    }]
                else:
                    source = "Not available"
                    breakdown = [{
                        "prefix": "Not available", "purchase_order_id": False,
                        "purchase_order_name": False, "suffix": "",
                    }]
                late = bool(
                    fully_covered and covering_date and target_date and covering_date > target_date
                )
                line.update({
                    "clearance_availability_qty": total_qty,
                    "clearance_availability_fully_covered": fully_covered,
                    "clearance_availability_date": covering_date,
                    "clearance_availability_late": late,
                    "clearance_availability_source": source,
                    "clearance_availability_status": (
                        "short" if not fully_covered else "late" if late else "available"
                    ),
                    "clearance_availability_breakdown": breakdown,
                    # held_qty, not total_qty — actually held/reserved
                    # right now, not the full forecast including future
                    # incoming (that's what the tooltip already narrates).
                    "clearance_reserved_qty": self._clearance_format_qty(held_qty),
                })

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
        # Batched once per compute call, not once per line — mirrors
        # sale.order._forecast_incoming_allocation's own batching
        # discipline, and matters for correctness here, not just
        # performance: a line's safety can only be judged against every
        # OTHER line simultaneously relying on the same committed incoming
        # pool (see _get_group_safe_future_replacement_lines), so this
        # must cover every candidate for the touched products, not just
        # whichever lines happen to be in `self` at this particular
        # compute call.
        candidate_product_ids = self.filtered(
            lambda l: l.is_scheduled_future_stock_release
            or l.move_ids.filtered(lambda m: m.state in ("assigned", "partially_available"))
        ).product_id.ids
        safe_ids = (
            self._get_group_safe_future_replacement_lines(product_ids=candidate_product_ids)
            if candidate_product_ids else set()
        )
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
            if held and line.id in safe_ids:
                line.clearance_defer_reason = "Scheduled Future Stock"
            else:
                line.clearance_defer_reason = False

    @api.model
    def _get_committed_future_incoming_moves_for_product(self, product, warehouse):
        """Incoming stock.move records for `product` at `warehouse`,
        sourced from a COMMITTED purchase order (state 'purchase' or
        'done' — never a draft/sent RFQ, which could still fall through
        and never actually arrive). This is deliberately a stricter bar
        than "any incoming move" — the whole point is a real safety
        guarantee, not a hopeful one.

        Product-and-warehouse-scoped generalization of
        _get_committed_future_incoming_moves below (which now delegates
        here) — used by sale.order._forecast_incoming_allocation to match
        committed incoming against ALL open demand for a product across
        every order competing for it, not just one line's own safety
        check.
        """
        return self.env["stock.move"].search([
            ("product_id", "=", product.id),
            ("picking_type_id", "=", warehouse.in_type_id.id),
            ("state", "not in", ("done", "cancel")),
            ("date", "!=", False),
            ("purchase_line_id.order_id.state", "in", ("purchase", "done")),
        ], order="date asc")

    def _get_committed_future_incoming_moves(self):
        self.ensure_one()
        return self._get_committed_future_incoming_moves_for_product(
            self.product_id, self.order_id.warehouse_id
        )

    def _has_safe_future_replacement(self):
        """True if this line can be trusted to safely give up its CURRENT
        stock (to an earlier-scheduled competitor), betting on a committed
        future incoming shipment to replace it in time.

        Thin per-line wrapper around _get_group_safe_future_replacement_lines
        — a line's safety can only be judged relative to every OTHER line
        simultaneously relying on the same committed incoming pool. Bug
        found live: two lines can each individually look covered by the
        same shipment that can only ever cover one of them, releasing
        real stock on a promise that was never actually there for both.
        """
        self.ensure_one()
        return self.id in self.env["sale.order.line"]._get_group_safe_future_replacement_lines(
            product_ids=[self.product_id.id]
        )

    def _is_scheduled_future_stock_candidate(self, cutoff):
        """Same eligibility gate _compute_clearance_defer_reason applies
        before ever considering this tag (not overridden, not an
        ineligible no_invoice order, has a scheduled date, not beyond the
        far-future cutoff) — re-derived here rather than shared, since
        _get_group_safe_future_replacement_lines must be self-sufficient
        (see its own docstring) and can't rely on the caller having
        already filtered its input."""
        self.ensure_one()
        order = self.order_id
        overridden = (
            order.is_reservation_hard_locked
            or self.is_reservation_hard_locked
            or self.is_force_reserved
        )
        ineligible = order.fulfillment_stage == "no_invoice" and not overridden
        if overridden or ineligible or not order.pick_scheduled_date:
            return False
        return order.pick_scheduled_date <= cutoff

    @api.model
    def _get_group_safe_future_replacement_lines(self, product_ids=None):
        """Which lines relying on a committed future incoming shipment —
        both already-released holders (is_scheduled_future_stock_release)
        and brand-new candidates still holding their own real stock right
        now — can actually be trusted to have the shared incoming pool
        cover them, once every other line with an equal or earlier claim
        has already taken its share first.

        Self-sufficient: re-derives the full candidate set via search
        rather than trusting a passed-in recordset, since a caller (e.g.
        a single line's own _has_safe_future_replacement) might only be
        looking at itself, but safety has to be judged against the
        COMPLETE set of lines competing for the same product's incoming
        pool — a caller-scoped view would just reintroduce the same
        isolated-check bug this method exists to close.

        Deliberately does NOT need to know about genuinely open (not
        currently held) demand: sale.order._clearance_move_priority
        already ranks this tier strictly ahead of ordinary open demand
        for any newly-arriving stock, so a holder that hasn't released
        yet isn't competing with open demand at all — only with every
        OTHER line making the same bet on this same pool. The moment a
        holder actually releases, it becomes open demand itself and is
        picked up correctly (and ranked first) by the existing
        _forecast_incoming_allocation / _reserve_by_clearance machinery —
        the two mechanisms hand off cleanly at that event.

        Returns a set of sale.order.line ids.
        """
        cutoff = fields.Datetime.now() + relativedelta(months=FAR_FUTURE_MONTHS)
        domain = [
            "|", ("is_scheduled_future_stock_release", "=", True),
            ("move_ids.state", "in", ("assigned", "partially_available")),
        ]
        if product_ids:
            domain = ["&"] + domain + [("product_id", "in", product_ids)]
        candidates = self.search(domain).filtered(
            lambda l: l._is_scheduled_future_stock_candidate(cutoff)
        )

        safe_ids = set()
        by_product_wh = {}
        for line in candidates:
            key = (line.product_id.id, line.order_id.warehouse_id.id)
            by_product_wh.setdefault(key, self.browse())
            by_product_wh[key] |= line

        for (product_id, wh_id), lines in by_product_wh.items():
            product = self.env["product.product"].browse(product_id)
            warehouse = self.env["stock.warehouse"].browse(wh_id)
            incoming = self._get_committed_future_incoming_moves_for_product(product, warehouse)
            if not incoming:
                continue
            remaining = {m.id: m.product_uom_qty for m in incoming}
            # Earliest-scheduled first — same rationale as
            # sale.order._clearance_move_priority's tier-0 ranking:
            # whoever needs their replacement soonest gets first claim on
            # the shared pool.
            for line in lines.sorted(key=lambda l: l.order_id.pick_scheduled_date or datetime.min):
                order = line.order_id
                target_date = order.pick_scheduled_date + timedelta(days=SCHEDULED_FUTURE_STOCK_MATCH_BUFFER_DAYS)
                on_or_before = incoming.filtered(lambda m: m.date <= target_date)
                match_candidates = on_or_before or incoming
                nearest = min(match_candidates, key=lambda m: abs((m.date - target_date).total_seconds()))

                # A confirmed shipment (container reference AND port
                # arrival date both on file — purchase_order.py's
                # is_receipt_confirmed) is real logistics data, not just
                # a plan — trusted with the tighter margin. Its move.date
                # already reflects port arrival + transit time once
                # confirmed (see purchase_order.py's
                # _sync_confirmed_receipt_date), so no separate basis
                # date is needed here. An unconfirmed one still needs
                # the wider margin, since its date is only ever a plan
                # that hasn't been locked down.
                required_buffer_days = (
                    SCHEDULED_FUTURE_STOCK_RELEASE_BUFFER_DAYS
                    if nearest.purchase_line_id.order_id.is_receipt_confirmed
                    else SCHEDULED_FUTURE_STOCK_RELEASE_BUFFER_DAYS_UNCONFIRMED
                )
                release_cutoff = nearest.date + timedelta(days=required_buffer_days)
                if order.pick_scheduled_date < release_cutoff:
                    continue

                up_to_nearest = incoming.filtered(lambda m: m.date <= nearest.date)
                if sum(remaining[m.id] for m in up_to_nearest) < line.product_uom_qty:
                    continue

                safe_ids.add(line.id)
                # Deduct THIS line's claim, earliest-dated shipment first,
                # before the next (lower-priority) line in this group gets
                # evaluated against what's left.
                need = line.product_uom_qty
                for m in up_to_nearest.sorted(key=lambda m: m.date):
                    if need <= 0:
                        break
                    take = min(need, remaining[m.id])
                    remaining[m.id] -= take
                    need -= take
        return safe_ids

    def write(self, vals):
        # Snapshot BEFORE super().write() — only lines whose value actually
        # flips get treated as a real transition; a redundant write of the
        # same value must never bump a line to the back of the locked-queue,
        # or re-run release cleanup, for no reason. Both directions —
        # applying AND releasing — need the override permission — folded in
        # here (used to live only in action_force_unlock_hard_lock) so the
        # boolean_toggle slider alone is enough to both lock and release.
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

        if (newly_locked or newly_unlocked) and not self.env.user.has_group(
            "clearance_reservation.group_reservation_override"
        ):
            raise AccessError(
                "You don't have permission to apply or release a reservation hard lock."
            )
        if (newly_force_reserved or newly_force_unreserved) and not self.env.user.has_group(
            "clearance_reservation.group_reservation_override"
        ):
            raise AccessError(
                "You don't have permission to apply or release a forced reservation."
            )

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
            # action_force_reserve, or one earned by genuinely reaching
            # Output (see sale_order.py's _clearance_is_output_move) —
            # those are separate protections, and Output's has no unlock
            # path at all.
            for line in newly_unlocked:
                locked_moves = line.move_ids.filtered(
                    lambda m: m.is_locked_reservation
                    and not line.is_force_reserved
                    and not self.env["sale.order"]._clearance_is_output_move(m)
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
            # hard lock, or one earned by genuinely reaching Output — those
            # are separate protections, and Output's has no unlock path at
            # all.
            for line in newly_force_unreserved:
                locked_moves = line.move_ids.filtered(
                    lambda m: m.is_locked_reservation
                    and not line.is_reservation_hard_locked
                    and not self.env["sale.order"]._clearance_is_output_move(m)
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
        # Checked here, up front — not left to the write() gate below —
        # because the actual reservation attempt below moves stock before
        # that write() ever happens; without this, an unauthorized user's
        # click would still reserve real stock and only fail afterward
        # when the is_force_reserved flag itself gets set, leaving the
        # reservation made but unflagged and unlocked.
        if not self.env.user.has_group("clearance_reservation.group_reservation_override"):
            raise AccessError("You don't have permission to force-reserve stock.")
        for line in self:
            # Set the flag FIRST, unconditionally — matching the user's
            # own intent, not computed afterward from a standalone
            # _action_assign() attempt (the previous design, and a real
            # bug: found live via the forecast report). That standalone
            # attempt could only ever grab stock nobody else was already
            # holding — it could never win this line the reassignment
            # queue's own blanket-release-then-reassign pass, since that
            # pass only runs AFTER this write (see write()'s own hook
            # below), and by then the flag would already be committed
            # False from the earlier, doomed-to-fail attempt. Setting it
            # True first means the write() hook's own _reserve_by_clearance
            # re-run (triggered synchronously by this same assignment)
            # evaluates this line as genuine T1 priority DURING the real
            # release, so it can properly displace an existing
            # LOWER-priority holder's stock — not just grab whatever
            # happened to already be free.
            line.is_force_reserved = True
            # Whatever that re-run just (re)assigned for this line —
            # lock it. Checked against ALL the line's moves, not just
            # ones that were unassigned before this call: a line with
            # abundant, uncontested stock is often already "assigned"
            # before anyone ever clicks this button, and the re-run's own
            # fast path can skip re-flagging a move that was already
            # sitting there correctly assigned. Includes
            # partially_available too, so a line that only partially won
            # the reassignment still gets what it did secure protected.
            assigned = line.move_ids.filtered(lambda m: m.state in ("assigned", "partially_available"))
            assigned.write({"is_locked_reservation": True})

    def action_force_unlock_reservation(self):
        self.ensure_one()
        if not self.env.user.has_group("clearance_reservation.group_reservation_override"):
            raise AccessError("You don't have permission to unlock a forced reservation.")
        self.move_ids.write({"is_locked_reservation": False})
        self.is_force_reserved = False
