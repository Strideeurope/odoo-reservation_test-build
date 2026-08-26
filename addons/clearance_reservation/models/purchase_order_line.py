from odoo import fields, models


class PurchaseOrderLine(models.Model):
    _inherit = "purchase.order.line"

    def write(self, vals):
        res = super().write(vals)
        # Native's own write() (purchase_stock) only ever pushes a
        # date_planned change onto the linked move's date_deadline —
        # never its actual date, which is the field this module's whole
        # reservation/forecast engine reads everywhere
        # (_get_committed_future_incoming_moves_for_product,
        # _forecast_incoming_allocation,
        # _get_group_safe_future_replacement_lines, stock_move.py's own
        # write() hook). Left alone, editing "Expected Arrival" by hand
        # silently has no effect on the forecast at all — the Receipt
        # column keeps showing whatever date the move was created with,
        # confirmed or not. Closing that gap here, for every line, keeps
        # date_planned and the move's own date always in agreement with
        # what the PO form actually shows.
        if "date_planned" in vals:
            new_date = fields.Datetime.to_datetime(vals["date_planned"])
            for line in self.filtered(lambda l: not l.display_type):
                line.move_ids.filtered(
                    lambda m: m.state not in ("done", "cancel")
                ).date = new_date
        return res
