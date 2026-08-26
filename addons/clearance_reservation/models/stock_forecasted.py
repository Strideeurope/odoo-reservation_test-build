from datetime import datetime

from odoo import models
from odoo.tools import format_date, format_datetime


class StockForecasted(models.AbstractModel):
    # stock.forecasted_product_template already inherits this model, so
    # extending it here also reaches the by-template forecast report —
    # listing both names would create a base-ordering conflict since
    # Template is already a descendant of Product.
    _inherit = "stock.forecasted_product_product"

    def _get_report_header(self, product_template_ids, product_ids, wh_location_ids):
        res = super()._get_report_header(product_template_ids, product_ids, wh_location_ids)
        # free_qty (on-hand minus reserved) only exists on product.product,
        # not product.template — fall back to the template's variants for
        # the by-template report, same as product_variants/product_variants_ids
        # already do just above in core.
        if product_template_ids:
            variants = self.env["product.template"].browse(product_template_ids).product_variant_ids
        elif product_ids:
            variants = self.env["product.product"].browse(product_ids)
        else:
            variants = self.env["product.product"]
        res["quantity_free"] = sum(variants.mapped("free_qty"))
        return res

    def _prepare_report_line(self, quantity, move_out=None, move_in=None,
                              replenishment_filled=True, product=False,
                              reserved_move=False, in_transit=False, read=True):
        line = super()._prepare_report_line(
            quantity, move_out=move_out, move_in=move_in,
            replenishment_filled=replenishment_filled, product=product,
            reserved_move=reserved_move, in_transit=in_transit, read=read,
        )
        document_out = line.get("document_out")
        if document_out and document_out["_name"] == "sale.order":
            order = self.env["sale.order"].browse(document_out["id"])
            document_out["clearance_date"] = (
                format_datetime(self.env, order.clearance_date)
                if order.clearance_date else False
            )
            # A fabricated (override) timestamp is never shown as if it
            # were a real payment moment — see forecasted_details_clearance.xml.
            document_out["clearance_is_override"] = order.clearance_is_override
            # Same fulfillment-stage badge already shown on the sale
            # order and the Pick/Ship transfer — surfaced here too so
            # it's visible without leaving the forecast. The label is
            # pre-computed server-side (see sale_order.py) rather than
            # re-derived in JS, so "No Invoice" vs "No Payment" can never
            # drift between the two surfaces.
            document_out["fulfillment_stage"] = order.fulfillment_stage
            document_out["fulfillment_stage_label"] = order.fulfillment_stage_label

        # Forecast twin of the lock_reason block below, but for stock that
        # hasn't arrived yet: native ins-matching (what decides
        # replenishment_filled/the native Reserve link for an unreserved
        # line) runs its own FCFS-ish ordering, blind to clearance
        # priority — and for this warehouse's layout, native "taken from
        # current stock"/"transit" reconciliation is dead code (see
        # _get_report_data below), so native ins-matching is the ONLY
        # thing left deciding whether an unreserved line looks
        # "coverable" at all. Attach our own, clearance-priority-correct
        # answer here so the JS can show it instead, and flag when native
        # would otherwise show a Reserve link that's actually driven by
        # that priority-blind matching rather than this module's queue.
        if move_out and not line.get("reservation"):
            sale_line = move_out.sale_line_id
            has_forecast = bool(sale_line and sale_line.expected_incoming_qty)
            is_late = (
                has_forecast and move_out.date and sale_line.expected_incoming_date
                and sale_line.expected_incoming_date > move_out.date
            )
            if has_forecast:
                breakdown = sale_line.expected_incoming_breakdown
                # The same entry expected_incoming_source/_date are
                # already drawn from (the latest-needed shipment) — its
                # id is what a clickable PO link needs. A split line
                # drawing from more than one PO can still only carry ONE
                # link here, same limitation native's own document_in
                # has (one move_in, one link, per report row).
                primary_po_id = breakdown[-1]["purchase_order_id"] if breakdown else False
                # True once that PO's container reference AND port
                # arrival date are both on file (purchase_order.py) — the
                # receive date is then treated as confirmed, not just
                # planned. Only the primary (latest-needed) shipment's
                # status is shown here, same one-PO-per-row limit as the
                # link itself.
                primary_confirmed = breakdown[-1]["purchase_order_confirmed"] if breakdown else False
                line["clearance_incoming_forecast"] = {
                    "qty": sale_line.expected_incoming_qty,
                    # Native's own Receipt column shows a date, not a
                    # datetime (format_date, not format_datetime) —
                    # matched here so a receipt date reads identically
                    # regardless of whether it came from native's own
                    # match or ours.
                    "date": format_date(self.env, sale_line.expected_incoming_date),
                    "source": sale_line.expected_incoming_source,
                    "purchase_order_id": primary_po_id,
                    "confirmed": primary_confirmed,
                    "fully_covers_remaining": sale_line.expected_incoming_fully_covers_remaining,
                    "is_late": is_late,
                }
            else:
                line["clearance_incoming_forecast"] = False
            # Native's own receipt_date/is_late (Receipt column) and
            # replenishment_filled (Used-by/Delivery columns' red
            # background) are ALL derived from the exact same priority-
            # blind ins-match this module distrusts (see _get_report_data
            # below for why "taken from stock"/"transit" reconciliation
            # is dead code on this warehouse's layout — ins-matching is
            # the only thing native has left to decide any of this for an
            # order-demand line). Replaced here with our own answer for
            # the receipt date (or none, if nothing is genuinely coming).
            # Explicit product decision: red is reserved for a line with
            # NEITHER real reserved quants NOR a committed future PO
            # against it — a real reservation is handled elsewhere
            # (line.reservation, already excluded from this whole block),
            # and a genuine forecast (however tight the timing) counts
            # as "covered" for colour purposes, white like a reservation
            # — "is_late" stays purely informational (the forecast
            # badge's tooltip), never a reason to flag red on its own.
            line["clearance_receipt_date"] = (
                format_date(self.env, sale_line.expected_incoming_date) if has_forecast else False
            )
            line["clearance_is_late_or_unavailable"] = not has_forecast
            # move_in being set on THIS call is exactly native's
            # ins-matching path (_reconcile_out_with_ins). Kept alongside
            # the two fields above (rather than relied on by itself) so
            # the JS can still tell, if it ever needs to, whether native
            # additionally rendered a (now-overridden) match of its own.
            line["clearance_reserve_is_misleading"] = bool(move_in)

        # move_out here is the actual demand move (the "out" that
        # _get_report_lines is building this line for), not the possibly-
        # different upstream `reserved_move` used just for the picking link.
        # Deliberately NOT restricted to an already-reserved state: a line
        # can be hard-locked (or force-reserved) with zero stock currently
        # available to it at all, in which case it renders as "Not
        # Available" with no reservation and no Unreserve button to anchor
        # a badge on — the lock is still real and worth surfacing there too,
        # it just hasn't caught anything yet. Whether to also hide/keep the
        # Reserve/Unreserve buttons is handled separately in JS, keyed off
        # line.reservation, not off this flag. Reuses stock.move's own
        # clearance_lock_reason (shared with the picking-form badge) rather
        # than re-deriving the same lock priority logic a second time here.
        if move_out and move_out.clearance_lock_reason:
            reason = move_out.clearance_lock_reason
            # A partially-fulfilled "Scheduled Future Stock" move splits
            # into TWO report lines here — the already-secured chunk
            # (this one, reservation truthy) and a separate line for the
            # still-unfulfilled remainder (reservation falsy). The badge
            # means "still waiting on something," true only of the
            # remainder — the secured chunk already has what it needs,
            # same reasoning as stock_move.py's move-level suppression
            # for a FULLY held line, just needed again here since a
            # partial move's own state ("partially_available") never
            # trips that check. Never suppressed for a genuine lock
            # (Force Reserved / Order or Product Hard Lock) — those
            # protect the reservation itself, secured chunk included.
            suppress_on_reserved_portion = reason == "Scheduled Future Stock" and line.get("reservation")
            if suppress_on_reserved_portion:
                return line
            line["lock_reason"] = reason
            # THIS line's own lock timestamp, not the order's blanket
            # queue_priority_bucket — an order can be force-reserved (or
            # hard-locked) on a completely different product and still show
            # up here for one of its other, unrelated lines; sorting by the
            # order-wide bucket would wrongly promote that unrelated line
            # too. See _get_report_data's sort_key.
            sale_line = move_out.sale_line_id
            order = sale_line.order_id
            if reason == "Order Hard Lock":
                line["lock_date"] = order.hard_lock_date
            elif reason == "Product Hard Lock":
                line["lock_date"] = sale_line.hard_lock_date
            elif reason == "Force Reserved":
                line["lock_date"] = sale_line.force_reserved_date
        return line

    def _get_report_moves_fields(self):
        # Needed to identify (and hide) a line whose move is sourced from
        # Output — see _get_report_data.
        return super()._get_report_moves_fields() + ["location_id"]

    def _get_report_data(self, product_template_ids=False, product_ids=False):
        res = super()._get_report_data(
            product_template_ids=product_template_ids, product_ids=product_ids
        )
        lines = res.get("lines")
        if not lines:
            return res

        # A line whose move is sourced FROM Output is the Ship leg of an
        # order whose Pick has already completed — the goods already
        # physically left Stock, so this is no longer "demand against
        # inventory" in any actionable sense for this report; it's just
        # noise once the Pick step is done. Also matches why that line
        # never gets a Reserve/Unreserve button anyway (native "in_transit"
        # gating — see forecasted_details_clearance.js).
        warehouse = self.env["stock.warehouse"].browse(
            self.env["stock.warehouse"]._get_warehouse_id_from_context()
        ) or self.env["stock.warehouse"].search([("active", "=", True)], limit=1)
        output_location_id = warehouse.wh_output_stock_loc_id.id

        def _is_from_output(line):
            move_out = line.get("move_out")
            location = move_out.get("location_id") if move_out else False
            if not location:
                return False
            loc_id = location[0] if isinstance(location, (list, tuple)) else location
            return loc_id == output_location_id

        # Both quirks below trace back to the same root cause: we scope
        # the Pick route's source to a sub-location (Picking Zone) rather
        # than the warehouse's own top-level Stock location. The native
        # report's reconciliation remaps every sub-location's on-hand
        # quantity UP to the top-level Stock key when first tallying
        # "free stock" — but then decrements reserved/consumed quantity
        # keyed by the MOVE's own location (Picking Zone) instead, a key
        # that was never populated by that remap. Two visible symptoms:
        #
        # 1. A "Free Stock in Transit" line with a zero-or-negative
        #    quantity and no actual outgoing demand attached — the
        #    decrement goes negative at the never-populated key,
        #    producing a phantom line. Never true "stock genuinely in
        #    motion between locations" (that would show positive here).
        def _is_phantom_transit(line):
            return line.get("in_transit") and not line.get("move_out") and line.get("quantity", 0) <= 0

        # 2. The one real "Free Stock" line (no move_out, no move_in, not
        #    in transit) is built from the SAME never-decremented figure
        #    at the top-level key — so it shows the raw total on hand,
        #    not what's actually left over after existing reservations.
        #    Corrected here against product.free_qty (on_hand - reserved),
        #    the same authoritative figure this report's own header
        #    already uses for "Unreserved" — so the two numbers agree.
        def _fix_free_stock_line(line):
            if line.get("move_out") or line.get("move_in") or line.get("in_transit"):
                return line
            product_id = line.get("product", {}).get("id")
            if not product_id:
                return line
            line["quantity"] = self.env["product.product"].browse(product_id).free_qty
            return line

        lines = [
            _fix_free_stock_line(line) for line in lines
            if not _is_from_output(line) and not _is_phantom_transit(line)
        ]

        # Native's own per-move ins/outs reconciliation can fragment ONE
        # move's still-open remaining demand into several separate report
        # rows — e.g. an 8-unit row it (misleadingly) considers "filled"
        # against a specific incoming move, plus a separate 21-unit row
        # for the rest — when in reality both pieces are the exact same
        # 29-unit remaining need from the exact same PO
        # (clearance_incoming_forecast is already identical across every
        # piece of the same move, since it's derived from the same
        # sale_line). Merged back into one row here, before any of the
        # sort/split logic below (which assumes at most one unreserved
        # row per move_out) — fragmenting a single real demand into
        # several rows is the same class of misleading native behavior
        # this module already corrects elsewhere, just at the row-count
        # level instead of the link/colour level. Never touches reserved
        # rows (a genuinely reserved move's own split-remainder handling
        # is separate, see further below) or lines with no move_out at
        # all (e.g. a bare Free Stock line).
        merged_by_move_id = {}
        merged_lines = []
        for line in lines:
            move_out = line.get("move_out")
            move_id = move_out.get("id") if move_out else None
            if line.get("reservation") or move_id is None:
                merged_lines.append(line)
                continue
            existing = merged_by_move_id.get(move_id)
            if existing is None:
                merged_by_move_id[move_id] = line
                merged_lines.append(line)
            else:
                existing["quantity"] = existing.get("quantity", 0) + line.get("quantity", 0)
                existing["clearance_reserve_is_misleading"] = bool(
                    existing.get("clearance_reserve_is_misleading") or line.get("clearance_reserve_is_misleading")
                )
        lines = merged_lines

        order_ids = {
            line["document_out"]["id"]
            for line in lines
            if line.get("document_out") and line["document_out"].get("_name") == "sale.order"
        }
        orders_by_id = {
            order.id: order for order in self.env["sale.order"].browse(order_ids)
        }

        def line_clearance_date(line):
            document_out = line.get("document_out")
            if document_out and document_out.get("_name") == "sale.order":
                order = orders_by_id.get(document_out["id"])
                if order and order.clearance_date:
                    return order.clearance_date
            return None

        def line_delivery_date(line):
            move_out = line.get("move_out")
            return move_out.get("date") if move_out else None

        # Python's sort is stable, so ties (including "no delivery date at
        # all") keep their original relative order.
        def by_delivery_date(line):
            return (0, line_delivery_date(line)) if line_delivery_date(line) else (1, "")

        # Explicit product decision: a line that currently holds no
        # reservation at all is ALWAYS sorted below every line that does,
        # full stop — clearance priority only means something once stock
        # is actually on the table; ranking an empty-handed line ahead of
        # a fulfilled one by clearance_date alone (which used to happen —
        # e.g. a far-future order excluded from reservation but with an
        # early clearance_date) was misleading. Reserved and unreserved
        # are therefore sorted as two separate blocks — except a split
        # line's own still-unfulfilled remainder (see the sibling
        # extraction below), which stays pinned directly beneath its own
        # reserved portion instead of sorting into the unreserved block
        # like an unrelated line.
        reserved_lines = [line for line in lines if line.get("reservation")]
        unreserved_lines = [line for line in lines if not line.get("reservation")]

        # Explicit product decision: once stock is actually in hand,
        # clearance priority has already done its job — it decided WHO
        # holds it. From here on, WHEN it's needed is what matters, so the
        # reserved block sorts by delivery date, not clearance date.
        reserved_sorted = sorted(reserved_lines, key=by_delivery_date)

        # A partially-fulfilled "Scheduled Future Stock" line (or any
        # other reserved-but-not-fully-covered move) produces a SECOND
        # report line for the exact same underlying move — the reserved
        # chunk here, and its still-unfulfilled remainder over in
        # unreserved_lines. Pulled out here and reunited with their
        # reserved counterpart below, rather than left to sort into the
        # unreserved block on their own — an incomplete line belongs
        # directly beneath its own complete portion, not scattered
        # wherever the unreserved block's own ordering would otherwise
        # put it.
        reserved_move_out_ids = {
            line["move_out"]["id"] for line in reserved_lines if line.get("move_out")
        }
        split_siblings_by_move_id = {}
        rest_after_siblings = []
        for line in unreserved_lines:
            move_out = line.get("move_out")
            move_id = move_out.get("id") if move_out else None
            if move_id is not None and move_id in reserved_move_out_ids:
                split_siblings_by_move_id.setdefault(move_id, []).append(line)
            else:
                rest_after_siblings.append(line)
        unreserved_lines = rest_after_siblings

        # Driven by THIS line's own lock_reason/lock_date/clearance (see
        # _prepare_report_line) rather than the order-wide
        # queue_priority_bucket — a bucket is an order-level aggregate that
        # can be "contaminated" by a completely unrelated line on the same
        # order (e.g. force-reserved for a different product), which would
        # wrongly promote a line that itself has no override at all.
        def clearance_sort_key(line):
            clearance_date = line_clearance_date(line)
            if clearance_date:
                return (0, clearance_date)
            return (99, datetime.max)

        # Explicit product decision: clearance timestamp is ALWAYS the
        # leading signal for who gets stock next — an order's place in
        # the queue is its place in the queue, whether or not it
        # currently holds anything yet. The unreserved block sorts by
        # the exact same clearance_sort_key as the reserved block above,
        # so the default view always mirrors the real allocation order
        # the engine will actually use (matching the client-side
        # "Clearance" sort toggle's own comment, which already assumed
        # this was the server's default). Delivery date remains
        # available as a genuinely different, informational view via the
        # "Delivery Date" toggle — needed-soonest is a useful question,
        # just never the one that decides who actually gets the stock.
        # The one exception: a force-reserved line with no clearance_date
        # at all (a no_invoice order, force-reserved only) still holds a
        # genuine, active claim on whatever stock arrives NEXT — the
        # engine ranks it ahead of every ordinary unreserved line
        # regardless of its own delivery date — so it floats to the very
        # front of this block instead of being sorted in by date.
        no_clearance_force_reserved = [
            line for line in unreserved_lines
            if line.get("lock_reason") == "Force Reserved" and line_clearance_date(line) is None
        ]
        no_clearance_force_reserved.sort(key=lambda line: line.get("lock_date") or datetime.max)
        no_clearance_ids = {id(line) for line in no_clearance_force_reserved}

        rest_unreserved = [line for line in unreserved_lines if id(line) not in no_clearance_ids]

        # "Scheduled Far Out" (sale_order.py's own FAR_FUTURE_MONTHS
        # exclusion) means this line has NO real claim on anything right
        # now, or for months to come, even though the order itself was
        # genuinely cleared (this defer reason never applies to a
        # no_invoice/uncleared order — _compute_clearance_defer_reason
        # already excludes those first). Sorting it in among ordinary
        # cleared lines by raw clearance_date alone (as used to happen)
        # could rank a far-future order ABOVE a nearer-term one it can
        # never actually compete with for stock — pulled out here into
        # its own block instead, placed below every genuinely competing
        # cleared line but still above uncleared/no_invoice orders
        # (explicit product decision: being cleared, however excluded
        # right now, still outranks never having had a real claim at
        # all) — see where this block lands in the final res["lines"]
        # assembly below.
        far_out_unreserved = [line for line in rest_unreserved if line.get("lock_reason") == "Scheduled Far Out"]
        far_out_ids = {id(line) for line in far_out_unreserved}
        rest_unreserved = [line for line in rest_unreserved if id(line) not in far_out_ids]

        # Within THIS remaining group, a genuinely cleared (paid) order
        # must still rank above one that was never even cleared — being
        # unreserved doesn't erase that difference. An uncleared order
        # has no clearance timestamp at all, so there's no queue position
        # to sort it by; it falls back to delivery date purely as an
        # informational tiebreaker among itself, well below every
        # cleared line regardless.
        cleared_unreserved = [line for line in rest_unreserved if line_clearance_date(line) is not None]
        uncleared_unreserved = [line for line in rest_unreserved if line_clearance_date(line) is None]

        cleared_unreserved_sorted = sorted(cleared_unreserved, key=clearance_sort_key)
        uncleared_unreserved_sorted = sorted(uncleared_unreserved, key=by_delivery_date)
        far_out_unreserved_sorted = sorted(far_out_unreserved, key=clearance_sort_key)

        # Reunite each reserved line with its own split-remainder
        # sibling(s), if any, placed directly beneath it — everything
        # else follows in the usual block order.
        reserved_block = []
        for line in reserved_sorted:
            reserved_block.append(line)
            move_out = line.get("move_out")
            move_id = move_out.get("id") if move_out else None
            reserved_block.extend(split_siblings_by_move_id.get(move_id, []))

        # far_out_unreserved sits below every genuinely competing line
        # (cleared or not) but still ABOVE uncleared/unpaid orders —
        # explicit product decision: a far-out order was at least
        # genuinely cleared (or is otherwise real, non-no_invoice)
        # demand, just excluded from the queue for being scheduled too
        # far ahead; an uncleared order has no legitimate claim on stock
        # at all regardless of scheduling, which ranks it lower still.
        res["lines"] = (
            reserved_block + no_clearance_force_reserved
            + cleared_unreserved_sorted + far_out_unreserved_sorted
            + uncleared_unreserved_sorted
        )
        return res
