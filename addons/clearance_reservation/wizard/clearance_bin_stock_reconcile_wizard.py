from odoo import fields, models
from odoo.exceptions import UserError


class ClearanceBinStockReconcileWizard(models.TransientModel):
    _name = "clearance.bin.stock.reconcile.wizard"
    _description = "Resolve a mismatch between tracked bin stock and real on-hand"

    product_id = fields.Many2one("product.product", required=True)
    bin_stock_total = fields.Float(related="product_id.bin_stock_total", readonly=True)
    reference_qty = fields.Float(related="product_id.bin_stock_reference_qty", readonly=True)
    discrepancy = fields.Float(related="product_id.bin_stock_discrepancy", readonly=True)
    resolution = fields.Selection(
        [
            ("fix_bin", "Correct a bin's tracked count — this was a logging mistake"),
            ("fix_stock", "Adjust real stock instead — this is a genuine physical loss/gain"),
        ],
        required=True, default="fix_stock",
    )
    # Only meaningful for fix_bin — which bin actually needs the
    # correction is something only a person can judge (the discrepancy
    # itself can never say WHICH bin is wrong, only that the total is
    # off), so this is never pre-filled from the discrepancy calculation.
    location_id = fields.Many2one(
        "stock.location", string="Bin to correct",
        domain=[("usage", "=", "internal")],
    )
    note = fields.Char()

    def action_confirm(self):
        self.ensure_one()
        discrepancy = self.discrepancy
        if not discrepancy:
            raise UserError("No discrepancy to resolve — the tracker already matches real stock.")

        if self.resolution == "fix_bin":
            if not self.location_id:
                raise UserError("Choose which bin's tracked count to correct.")
            record = self.env["clearance.bin.stock"]._get_or_create(
                self.product_id, self.location_id
            )
            note = self.note or "Reconciliation"
            # discrepancy = tracked_total - real_stock. Positive means the
            # tracker claims more than genuinely exists — bring it down by
            # removing the excess from the chosen bin. Negative means the
            # tracker is missing stock that really exists — add it.
            if discrepancy > 0:
                record.remove_quantity(discrepancy, note=note)
            else:
                record.add_quantity(-discrepancy, note=note)
        else:
            # Adjust REAL stock to match the tracker — a real, validated
            # inventory adjustment on Main (the only location real quants
            # exist at under this model), never raw SQL. Trusts the
            # tracker as correct in this case, by the user's own
            # judgment that this reflects a genuine physical change.
            main_location = self._get_main_location()
            quant = self.env["stock.quant"].search([
                ("product_id", "=", self.product_id.id),
                ("location_id", "=", main_location.id),
            ], limit=1)
            if not quant:
                quant = self.env["stock.quant"].create({
                    "product_id": self.product_id.id,
                    "location_id": main_location.id,
                })
            quant.with_context(inventory_mode=True).write({
                "inventory_quantity": quant.quantity + discrepancy,
            })
            quant.action_apply_inventory()

        return {"type": "ir.actions.act_window_close"}

    def _get_main_location(self):
        """The product's own existing picking sub-location under the
        Pick route's source, matching the "one dedicated picking
        location per product" layout — never the flat parent zone
        itself, which would scatter the adjustment away from where a
        picker actually looks for this product (the same bug found and
        fixed once already for auto-replenishment). Falls back to the
        parent only when the product has no picking sub-location on
        record at all yet."""
        self.ensure_one()
        warehouse = self.env["stock.warehouse"].search([], limit=1)
        pick_rule = self.env["stock.rule"].search(
            [("picking_type_id", "=", warehouse.pick_type_id.id)], limit=1
        )
        picking_zone = pick_rule.location_src_id or warehouse.lot_stock_id
        existing = self.env["stock.quant"].search([
            ("product_id", "=", self.product_id.id),
            ("location_id", "child_of", picking_zone.id),
            ("location_id", "!=", picking_zone.id),
        ], limit=1)
        return existing.location_id if existing else picking_zone
