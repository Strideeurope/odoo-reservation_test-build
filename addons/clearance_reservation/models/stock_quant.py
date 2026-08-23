from odoo import models
from odoo.exceptions import UserError


class StockQuant(models.Model):
    _inherit = "stock.quant"

    def _apply_inventory(self):
        res = super()._apply_inventory()
        # Any manual inventory count changes what's actually on hand for a
        # product — up (more to hand out) or down (a physical count came in
        # short, and whoever is currently holding a now-oversubscribed
        # reservation may need to give some of it up). Either direction is
        # handled correctly by the same release-then-reassign logic in
        # _reserve_by_clearance: it re-derives the right allocation from
        # scratch against current on-hand, so a downward count naturally
        # reallocates the now-scarcer stock to whoever has priority.
        #
        # Scoped to just the product(s) actually counted here — never a
        # full, unscoped run — since stock is reserved per product and an
        # adjustment to one product has no bearing on any other.
        product_ids = self.product_id.ids
        if product_ids:
            self.env["sale.order"]._reserve_by_clearance(product_ids=product_ids)
        return res

    def _clearance_locked_moves(self):
        """Moves currently holding a reservation against exactly these
        quants (matched the same way Odoo itself matches a quant: product,
        location, lot, package, owner) that are hard-locked or soft-locked
        (force-reserved). Used to stop move_quants below from doing to a
        locked reservation what _do_unreserve/_action_cancel already
        refuse to do."""
        if not self:
            return self.env["stock.move"]
        quant_keys = {
            (q.product_id.id, q.location_id.id, q.lot_id.id, q.package_id.id, q.owner_id.id)
            for q in self
        }
        candidate_lines = self.env["stock.move.line"].search([
            ("state", "in", ("assigned", "partially_available")),
            ("product_id", "in", self.product_id.ids),
            ("location_id", "in", self.location_id.ids),
        ])
        matching_lines = candidate_lines.filtered(
            lambda l: (l.product_id.id, l.location_id.id, l.lot_id.id, l.package_id.id, l.owner_id.id)
            in quant_keys
        )
        return matching_lines.move_id

    def move_quants(self, location_dest_id=False, package_dest_id=False, message=False, unpack=False):
        # This is the method behind Odoo's native "Relocate" quant action —
        # the tool for reorganizing bins, including already-reserved stock.
        # It happily moves a fully-reserved quant with no error, but as a
        # side effect it silently drops whatever reservation was on it (the
        # move it was reserved against reverts to unreserved) — bypassing
        # _do_unreserve entirely, and with it the hard-lock/force-reserve
        # protection every other unreserve path in this module already
        # enforces. Mirrors _do_unreserve's own two-tier check exactly:
        # hard lock never bypassable, soft lock (force-reserve) bypassable
        # only via the same force_unreserve_override context every other
        # override path already uses.
        locked_moves = self._clearance_locked_moves()
        hard_locked = locked_moves.filtered(
            lambda m: m.sale_line_id.order_id.is_reservation_hard_locked
            or m.sale_line_id.is_reservation_hard_locked
        )
        if hard_locked:
            raise UserError(
                "This stock is reserved by a hard-locked order (or a "
                "hard-locked product on it) and cannot be relocated under "
                "any circumstance: %s"
                % ", ".join(hard_locked.mapped("product_id.display_name"))
            )
        soft_locked = locked_moves.filtered("is_locked_reservation") - hard_locked
        if soft_locked and not self.env.context.get("force_unreserve_override"):
            raise UserError(
                "This stock is locked (force-reserved) and cannot be "
                "relocated: %s" % ", ".join(soft_locked.mapped("product_id.display_name"))
            )

        # For every OTHER case — an ordinary, unlocked reservation — the
        # relocation is allowed through as native Odoo already does. As a
        # side effect it still silently drops that reservation, so without
        # the re-run below a warehouse staffer relocating reserved stock
        # would leave that order silently unreserved until some unrelated
        # event happened to touch the same product. Re-running the queue
        # here means the order automatically reclaims its reservation at
        # the new location on this same pass — no manual cleanup, no
        # silent gap. Also what backs sale_order.py's own instant
        # Buffer-to-Picking top-up, which calls this same method
        # internally; the existing _within_reserve_by_clearance recursion
        # guard on that method prevents this from double-firing in that
        # case.
        product_ids = self.product_id.ids
        res = super().move_quants(
            location_dest_id=location_dest_id, package_dest_id=package_dest_id,
            message=message, unpack=unpack,
        )
        if product_ids:
            self.env["sale.order"]._reserve_by_clearance(product_ids=product_ids)
        return res
