from datetime import datetime, timedelta

from odoo import api, fields, models

# Fixed transit time from the harbor (port_arrival_date) to the actual
# warehouse — explicit product decision, not derived from any carrier/
# route data. Only meaningful once is_receipt_confirmed is True; an
# unconfirmed PO has no port_arrival_date to add this to at all.
GOODS_TRANSIT_DAYS = 10


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    container_reference = fields.Char(string="Container Reference")
    # Date, not Datetime, to match how this is actually tracked in
    # shipping/logistics paperwork — day-level, unlike date_planned
    # (Datetime), which is the scheduling value the rest of this module
    # already reads everywhere (stock.move.date).
    port_arrival_date = fields.Date(string="Port Arrival Date")
    is_receipt_confirmed = fields.Boolean(
        compute="_compute_is_receipt_confirmed", store=True,
        help="True once both the container reference and port arrival "
             "date are filled in — the receive date is then treated as "
             "confirmed, not just planned.",
    )

    @api.depends("container_reference", "port_arrival_date")
    def _compute_is_receipt_confirmed(self):
        for order in self:
            order.is_receipt_confirmed = bool(order.container_reference and order.port_arrival_date)

    @api.model_create_multi
    def create(self, vals_list):
        orders = super().create(vals_list)
        orders._sync_confirmed_receipt_date()
        return orders

    def write(self, vals):
        res = super().write(vals)
        if "container_reference" in vals or "port_arrival_date" in vals:
            self._sync_confirmed_receipt_date()
        return res

    def _sync_confirmed_receipt_date(self):
        """Once both the container reference and port arrival date are on
        file, the goods' real expected arrival at the warehouse is the
        port arrival date plus the fixed harbor-to-warehouse transit time
        (GOODS_TRANSIT_DAYS) — more accurate real logistics data than
        whatever date_planned held before confirmation. No separate
        field for this: pushed straight onto the PO line's own
        date_planned, exactly as if a user had edited "Expected Arrival"
        by hand — purchase_order_line.py's own write() override then
        propagates that onto the linked incoming move's own date too
        (never just date_deadline, which is all native's own write()
        touches), the field this module's whole reservation/forecast
        engine actually reads.
        """
        for order in self:
            if not order.is_receipt_confirmed:
                continue
            new_date = datetime.combine(
                order.port_arrival_date + timedelta(days=GOODS_TRANSIT_DAYS),
                datetime.min.time(),
            )
            order.order_line.filtered(lambda l: not l.display_type).date_planned = new_date
