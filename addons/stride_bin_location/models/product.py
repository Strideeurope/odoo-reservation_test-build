from odoo import models
from odoo.osv import expression


class ProductProduct(models.Model):
    _inherit = "product.product"

    def _get_domain_locations_new(self, location_ids):
        loc_domain, dest_loc_domain, dest_loc_domain_out = super()._get_domain_locations_new(location_ids)
        exclude_bin_src = [("location_id.usage", "!=", "bin")]
        exclude_bin_dest = [("location_dest_id.usage", "!=", "bin")]
        return (
            expression.AND([loc_domain, exclude_bin_src]),
            expression.AND([dest_loc_domain, exclude_bin_dest]),
            expression.AND([dest_loc_domain_out, exclude_bin_src]),
        )
