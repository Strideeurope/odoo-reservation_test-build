from odoo import api, fields, models
from odoo.exceptions import UserError


class ClearanceBinStock(models.Model):
    _name = "clearance.bin.stock"
    _description = (
        "Physical stock tracked per bin — informational only. Deliberately "
        "NOT a stock.quant and never read by _reserve_by_clearance: this "
        "answers 'how much of this product is physically at this bin', "
        "not 'how much can be reserved'. Odoo's own stock ledger (kept "
        "entirely at the Main picking location) is the only thing that "
        "governs reservation."
    )
    _inherit = ["mail.thread"]
    _rec_name = "location_id"

    product_id = fields.Many2one(
        "product.product", required=True, ondelete="cascade", tracking=True,
    )
    location_id = fields.Many2one(
        "stock.location", required=True, ondelete="cascade", tracking=True,
        domain=[("usage", "=", "internal")],
    )
    # Changed only through add_quantity/remove_quantity below — never
    # edited directly — so the chatter always carries a real, attributable
    # reason for every change, the same way a stock.quant's reserved
    # quantity is never hand-edited either.
    quantity = fields.Float(default=0.0, tracking=True, readonly=True)

    _sql_constraints = [
        (
            "product_location_uniq",
            "unique(product_id, location_id)",
            "Only one tracked-stock record per product per bin.",
        ),
    ]

    @api.model
    def _get_or_create(self, product, location):
        record = self.search([
            ("product_id", "=", product.id),
            ("location_id", "=", location.id),
        ], limit=1)
        return record or self.create({
            "product_id": product.id, "location_id": location.id,
        })

    def add_quantity(self, qty, note=None):
        """Log a pallet (or partial quantity) physically PLACED at this
        bin — e.g. after receiving, or after carrying overstock back from
        Main. The only way this model's quantity ever increases."""
        self.ensure_one()
        if qty <= 0:
            raise UserError("Quantity to place must be greater than zero.")
        self.quantity += qty
        detail = f" — {note}" if note else ""
        self.message_post(body=(
            f"Placed {qty} × {self.product_id.display_name} at "
            f"{self.location_id.display_name}{detail}."
        ))

    def remove_quantity(self, qty, note=None):
        """Log a pallet (or partial quantity) physically REMOVED from
        this bin — e.g. carried over to restock Main's shelf. Blocks
        removing more than is currently tracked here, since that can
        only mean the tracker is already out of sync with reality and
        needs a person to look at it, not a silent negative count."""
        self.ensure_one()
        if qty <= 0:
            raise UserError("Quantity to remove must be greater than zero.")
        if qty > self.quantity:
            raise UserError(
                f"Only {self.quantity} × {self.product_id.display_name} is "
                f"tracked at {self.location_id.display_name} — cannot "
                f"remove {qty}."
            )
        self.quantity -= qty
        detail = f" — {note}" if note else ""
        self.message_post(body=(
            f"Removed {qty} × {self.product_id.display_name} from "
            f"{self.location_id.display_name}{detail}."
        ))
