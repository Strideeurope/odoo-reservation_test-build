from datetime import datetime

from odoo import models
from odoo.tools import format_datetime


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

        # Explicit product decision: a line that currently holds no
        # reservation at all is ALWAYS sorted below every line that does,
        # full stop — clearance priority only means something once stock
        # is actually on the table; ranking an empty-handed line ahead of
        # a fulfilled one by clearance_date alone (which used to happen —
        # e.g. a far-future order excluded from reservation but with an
        # early clearance_date) was misleading. Reserved and unreserved
        # are therefore sorted as two fully separate blocks, never
        # interleaved by comparing across them.
        reserved_lines = [line for line in lines if line.get("reservation")]
        unreserved_lines = [line for line in lines if not line.get("reservation")]

        # Driven by THIS line's own lock_reason/lock_date/clearance (see
        # _prepare_report_line) rather than the order-wide
        # queue_priority_bucket — a bucket is an order-level aggregate that
        # can be "contaminated" by a completely unrelated line on the same
        # order (e.g. force-reserved for a different product), which would
        # wrongly promote a line that itself has no override at all.
        #
        # Neither hard lock nor force-reserve get their own jump-the-line
        # tier here — a locked or force-reserved line with a genuine
        # clearance_date of its own sorts by that, exactly like an ordinary
        # line ("keeps its place in line"), mirroring the engine's own
        # ranking where an override grants no priority over anyone with a
        # real, earlier claim. Only meaningful within the reserved block —
        # every line here already has stock, this just orders WHEN it was
        # (or would have been) their fair turn.
        def clearance_sort_key(line):
            clearance_date = line_clearance_date(line)
            if clearance_date:
                return (0, clearance_date)
            return (99, datetime.max)

        reserved_sorted = sorted(reserved_lines, key=clearance_sort_key)

        # Within the UNRESERVED block, clearance priority isn't actionable
        # information yet — nothing has happened. What IS useful is which
        # one is needed soonest, so this block sorts by delivery date
        # instead (same field as the client-side "Delivery Date" toggle).
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
        # Python's sort is stable, so ties (including "no delivery date at
        # all") keep their original relative order.
        rest_unreserved_sorted = sorted(
            rest_unreserved,
            key=lambda line: (0, line_delivery_date(line)) if line_delivery_date(line) else (1, ""),
        )

        res["lines"] = reserved_sorted + no_clearance_force_reserved + rest_unreserved_sorted
        return res
