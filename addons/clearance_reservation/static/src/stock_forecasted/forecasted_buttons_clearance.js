/** @odoo-module **/
import { ForecastedButtons } from "@stock/stock_forecasted/forecasted_buttons";
import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

patch(ForecastedButtons.prototype, {
    setup() {
        super.setup();
        this.notification = useService("notification");
    },

    async _onClickReserveByClearance() {
        const result = await this.orm.call(this.resModel, "action_reserve_by_clearance", [
            [this.productId],
        ]);
        this.notification.add(
            result.reserved_move_count
                ? _t("Reserved %(count)s move(s) across %(orders)s order(s) in the clearance queue.", {
                      count: result.reserved_move_count,
                      orders: result.order_count,
                  })
                : _t("Nothing new to reserve for this product right now."),
            { type: result.reserved_move_count ? "success" : "info" }
        );
        return this.props.reloadReport();
    },
});
