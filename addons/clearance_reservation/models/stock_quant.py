from odoo import models


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
