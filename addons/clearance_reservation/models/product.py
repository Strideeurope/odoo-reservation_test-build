from odoo import models


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
