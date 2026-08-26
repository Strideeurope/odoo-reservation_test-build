import { registry } from "@web/core/registry";
import { Component, onWillDestroy } from "@odoo/owl";
import { usePopover } from "@web/core/popover/popover_hook";
import { useService } from "@web/core/utils/hooks";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

// Grace period between leaving the trigger and the popover actually
// closing — long enough for the mouse to cross the visual gap between
// the "Reserved" text and the popover box without it vanishing before
// the mouse ever reaches it. Leaving the popover itself, once reached,
// closes immediately — no gap left to cross by then.
const CLOSE_DELAY_MS = 750;
// Delay before the popover OPENS at all — avoids a flash on a quick
// mouse pass over "Reserved" that was never meant to linger there.
const OPEN_DELAY_MS = 250;

// Same red/amber/green language the forecast report's own badges already
// use — plain font colour here (text-success/warning/danger), no badge
// pill, since this now sits right next to Quantity as a plain number
// ("Reserved"), not a standalone status word.
const STATUS_CLASS = {
    available: "text-success",
    late: "text-warning",
    short: "text-danger",
};

function statusClassFor(record) {
    return STATUS_CLASS[record.data.clearance_availability_status] || "text-muted";
}

// Mirrors sale_stock's own QtyAtDatePopover (native's "click the icon next
// to Quantity" popover) — same usePopover mechanism, same small
// table-of-rows layout — but reading this module's own clearance-priority
// -correct numbers instead of native's priority-blind ones, and opened on
// HOVER rather than a click.
export class ClearanceAvailabilityPopover extends Component {
    static template = "clearance_reservation.ClearanceAvailabilityPopover";
    // usePopover always passes its own close() through as a prop
    // (mirrors sale_stock's own QtyAtDatePopover props exactly) — must
    // be declared even though this template never calls it itself,
    // since OWL's strict prop validation rejects an undeclared key.
    // onMouseEnter/onMouseLeave are this module's own addition, wired to
    // the SAME cancel/schedule-close logic the trigger itself uses, so
    // hovering onto the popover (to click a link in it) keeps it open.
    static props = { record: Object, close: Function, onMouseEnter: Function, onMouseLeave: Function };

    setup() {
        this.actionService = useService("action");
    }

    // Green if the forecast covers it in time for the order's own
    // delivery date (or it's already available now), orange if covered
    // only after that date, red if not covered at all — same tiering
    // clearance_availability_status already carries for the Reserved
    // number itself.
    get sourceClass() {
        return statusClassFor(this.props.record);
    }

    openPurchaseOrder(purchaseOrderId) {
        this.actionService.doAction({
            type: "ir.actions.act_window",
            res_model: "purchase.order",
            res_id: purchaseOrderId,
            views: [[false, "form"]],
            target: "current",
        });
    }

    // Identical target/context to sale_stock's own QtyAtDatePopover
    // openForecast() — same deep link, same warehouse_id/move_ids
    // helper fields native's own (now-hidden) widget already declares
    // in this same view, so nothing extra needs loading for this.
    openForecast() {
        this.actionService.doAction("stock.stock_forecasted_product_product_action", {
            additionalContext: {
                active_model: "product.product",
                active_id: this.props.record.data.product_id[0],
                warehouse_id: this.props.record.data.warehouse_id && this.props.record.data.warehouse_id[0],
                move_to_match_ids: (this.props.record.data.move_ids?.records || []).map((r) => r.resId),
                sale_line_to_match_id: this.props.record.resId,
            },
        });
    }
}

export class ClearanceAvailabilityField extends Component {
    static template = "clearance_reservation.ClearanceAvailabilityField";
    static components = { Popover: ClearanceAvailabilityPopover };
    static props = { ...standardFieldProps };

    setup() {
        this.popover = usePopover(this.constructor.components.Popover, { position: "top" });
        this.closeTimer = null;
        this.openTimer = null;
        onWillDestroy(() => {
            this.cancelScheduledClose();
            this.cancelScheduledOpen();
        });
    }

    get value() {
        return this.props.record.data[this.props.name] || "";
    }
    get statusClass() {
        return statusClassFor(this.props.record);
    }

    cancelScheduledClose() {
        if (this.closeTimer) {
            clearTimeout(this.closeTimer);
            this.closeTimer = null;
        }
    }
    scheduleClose() {
        this.cancelScheduledClose();
        this.closeTimer = setTimeout(() => {
            this.popover.close();
            this.closeTimer = null;
        }, CLOSE_DELAY_MS);
    }

    cancelScheduledOpen() {
        if (this.openTimer) {
            clearTimeout(this.openTimer);
            this.openTimer = null;
        }
    }
    doOpen(target) {
        this.popover.open(target, {
            record: this.props.record,
            onMouseEnter: () => this.cancelScheduledClose(),
            // Leaving the popover itself closes right away — by then
            // there's no gap left to cross.
            onMouseLeave: () => {
                this.cancelScheduledClose();
                this.popover.close();
            },
        });
    }

    onMouseEnter(ev) {
        this.cancelScheduledClose();
        if (!this.value || this.popover.isOpen || this.openTimer) {
            return;
        }
        const target = ev.currentTarget;
        this.openTimer = setTimeout(() => {
            this.openTimer = null;
            this.doOpen(target);
        }, OPEN_DELAY_MS);
    }
    onMouseLeave() {
        this.cancelScheduledOpen();
        this.scheduleClose();
    }
}

registry.category("fields").add("clearance_availability", {
    component: ClearanceAvailabilityField,
    supportedTypes: ["char"],
});
