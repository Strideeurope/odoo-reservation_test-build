from odoo import api, fields, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    def action_reserve_by_clearance(self):
        return self.env["sale.order"]._reserve_by_clearance(
            product_ids=self.product_variant_ids.ids
        )


class ProductProduct(models.Model):
    _inherit = "product.product"

    def action_reserve_by_clearance(self):
        return self.env["sale.order"]._reserve_by_clearance(product_ids=self.ids)

    # Purely informational — never read by _reserve_by_clearance. The sum
    # of every bin someone has logged this product at, compared against
    # Odoo's own real total under the warehouse's top-level Stock location
    # (Main plus any sub-location still holding a real quant) — NOT the
    # product's blanket qty_available, which also counts stock already
    # picked and sitting in Output awaiting shipment; that stock is no
    # longer anyone's "where do I find it on a bin" question, so including
    # it here would produce a permanent, meaningless discrepancy. Same
    # location scoping already used by stock_move.py's
    # total_warehouse_qty_available. When the two agree, every physical
    # placement/removal has been logged correctly; a mismatch is the
    # signal to go investigate (either the tracker missed an update, or
    # there's a genuine physical loss to fix via a real inventory
    # adjustment on Main).
    bin_stock_total = fields.Float(compute="_compute_bin_stock_total")
    # The actual figure bin_stock_total is compared against — surfaced as
    # its own field (not just baked into bin_stock_discrepancy) so the
    # comparison is never ambiguous on screen. Odoo's native "On Hand"
    # field elsewhere on the product form shows a DIFFERENT, larger
    # number (qty_available, which also includes stock already picked
    # and sitting in Output) — comparing the tracked total against THAT
    # instead of this one produces a meaningless gap.
    bin_stock_reference_qty = fields.Float(compute="_compute_bin_stock_total")
    bin_stock_discrepancy = fields.Float(compute="_compute_bin_stock_total")
    bin_stock_count = fields.Integer(compute="_compute_bin_stock_total")

    @api.depends()
    def _compute_bin_stock_total(self):
        # Not a real dependency-tracked compute (clearance.bin.stock isn't
        # a related/stored field on this model) — deliberately recomputed
        # fresh every time it's displayed, same as any other live on-hand
        # figure in this module.
        bin_stock = self.env["clearance.bin.stock"].search([
            ("product_id", "in", self.ids)
        ])
        by_product = {}
        for record in bin_stock:
            by_product.setdefault(record.product_id.id, []).append(record)
        warehouse = self.env["stock.warehouse"].search([], limit=1)
        for product in self:
            records = by_product.get(product.id, [])
            product.bin_stock_count = len(records)
            product.bin_stock_total = sum(r.quantity for r in records)
            stock_qty = (
                product.with_context(location=warehouse.lot_stock_id.id).qty_available
                if warehouse else product.qty_available
            )
            product.bin_stock_reference_qty = stock_qty
            product.bin_stock_discrepancy = product.bin_stock_total - stock_qty

    def action_view_bin_stock(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": f"Bin Stock — {self.display_name}",
            "res_model": "clearance.bin.stock",
            "view_mode": "list,form",
            "domain": [("product_id", "=", self.id)],
            "context": {"default_product_id": self.id},
        }
