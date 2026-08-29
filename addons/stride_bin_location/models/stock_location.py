from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class StockLocation(models.Model):
    _inherit = "stock.location"

    usage = fields.Selection(
        selection_add=[("bin", "Bin (Shadow Ledger)")],
        ondelete={"bin": "cascade"},
    )
    stock_sublocation_id = fields.Many2one(
        "stock.location",
        string="Mirrored Stock Sublocation",
        copy=False,
        help="The SWH/STOCK sublocation this bin is the shadow ledger for. "
        "This link is the single source of truth for the mirrored pair — "
        "matching names are convention for readability only, never parsed.",
    )

    _sql_constraints = [
        (
            "stock_sublocation_id_uniq",
            "unique(stock_sublocation_id)",
            "Each SWH/STOCK sublocation can be mirrored by at most one bin.",
        ),
    ]

    @api.constrains("stock_sublocation_id", "usage", "barcode")
    def _check_bin_mirror(self):
        for location in self:
            if location.usage == "bin" and location.barcode:
                raise ValidationError(_(
                    "A bin location cannot carry a barcode — barcodes live only "
                    "on the SWH/STOCK side of a mirrored pair."
                ))
            if not location.stock_sublocation_id:
                continue
            if location.usage != "bin":
                raise ValidationError(_(
                    "Only a bin-usage location can be linked to a mirrored stock sublocation."
                ))
            if location.stock_sublocation_id == location:
                raise ValidationError(_("A location cannot mirror itself."))
            if location.stock_sublocation_id.usage == "bin":
                raise ValidationError(_(
                    "A bin's mirrored sublocation must not itself be a bin location."
                ))

    @api.model
    def _get_or_create_bin_mirror(self, stock_sublocation, bin_parent):
        """Find-or-create the SWH/BIN counterpart of a SWH/STOCK sublocation (W2).

        The only place a bin is ever created for a mirrored pair — humans and
        other code must never create either half manually.
        """
        existing = self.sudo().search(
            [("stock_sublocation_id", "=", stock_sublocation.id)], limit=1
        )
        if existing:
            return existing
        return self.sudo().create({
            "name": stock_sublocation.name,
            "location_id": bin_parent.id,
            "usage": "bin",
            "company_id": stock_sublocation.company_id.id,
            "stock_sublocation_id": stock_sublocation.id,
        })

    def _get_mirror_location(self):
        self.ensure_one()
        if self.usage == "bin":
            return self.stock_sublocation_id
        return self.sudo().search([("stock_sublocation_id", "=", self.id)], limit=1)

    def write(self, vals):
        res = super().write(vals)
        if "name" in vals and not self.env.context.get("skip_mirror_rename"):
            for location in self:
                mirror = location._get_mirror_location()
                if mirror and mirror.name != vals["name"]:
                    mirror.with_context(skip_mirror_rename=True).write({"name": vals["name"]})
        return res
