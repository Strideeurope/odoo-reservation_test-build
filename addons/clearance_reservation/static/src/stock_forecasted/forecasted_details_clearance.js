/** @odoo-module **/
import { ForecastedDetails } from "@stock/stock_forecasted/forecasted_details";
import { patch } from "@web/core/utils/patch";
import { useState } from "@odoo/owl";

patch(ForecastedDetails.prototype, {
    setup() {
        super.setup();
        this.clearanceSortState = useState({ mode: "clearance" });
    },

    _onClickSortMode(mode) {
        this.clearanceSortState.mode = mode;
    },

    // Native Reserve/Unreserve visibility is driven by whether native's
    // own priority-blind ins-matching happened to mark this line
    // "coverable" (see stock_forecasted.py's _prepare_report_line) — for
    // an unreserved line, clicking it would reserve whatever native
    // matched, not whatever this module's clearance queue would actually
    // pick. Suppressed there in favor of the read-only forecast badge;
    // left alone everywhere else (e.g. the real Unreserve link on an
    // already-reserved line, which this flag is never set for).
    displayReserve(line) {
        if (line.clearance_reserve_is_misleading) {
            return false;
        }
        return super.displayReserve(line);
    },

    // "Locked" here means an active override with a real claim — hard
    // lock or force-reserve — never "Scheduled Far Out" or "Scheduled
    // Future Stock", which are the opposite of locked (a line explicitly
    // excluded from, or that gave up, its reservation).
    _isLockedLine(line) {
        return !!line.lock_reason && !this._isDeferReason(line.lock_reason);
    },

    _isDeferReason(reason) {
        return reason === "Scheduled Far Out" || reason === "Scheduled Future Stock";
    },
    _lockBadgeClass(reason) {
        // Distinct from "Scheduled Future Stock" (text-bg-warning, an
        // ACTIVE protected claim expecting a shipment back) — "Scheduled
        // Far Out" is the opposite: no claim on anything at all, for
        // months. text-bg-info reads as "informational, inactive"
        // rather than "watch this, something's pending".
        if (reason === "Scheduled Far Out") {
            return "text-bg-info";
        }
        return this._isDeferReason(reason) ? "text-bg-warning" : "text-bg-secondary";
    },
    _lockIcon(reason) {
        if (reason === "Scheduled Far Out") return "fa-calendar-o";
        if (reason === "Scheduled Future Stock") return "fa-truck";
        return "fa-lock";
    },
    _lockTitle(reason) {
        if (reason === "Scheduled Far Out") {
            return "Not reserved: order is scheduled more than 6 months out";
        }
        if (reason === "Scheduled Future Stock") {
            return "Gave up its current stock to an earlier-scheduled order — a committed future incoming shipment is confirmed to cover it in time, and it now has priority to reclaim that shipment first";
        }
        return "This line's reservation cannot be released by any process";
    },

    // Same stages/colors as the badge already shown on the sale order
    // and the Pick/Ship transfer. The label itself is pre-computed
    // server-side (sale_order.py's fulfillment_stage_label) rather than
    // re-derived here, so "No Invoice" vs "No Payment" can never drift
    // between the two surfaces — this only decides color, keyed off the
    // label for the no_invoice case specifically (red for a genuine
    // no-payment situation, grey for one that's never even been
    // invoiced) and off the raw stage for the other three.
    _stageBadgeClass(stage, label) {
        if (stage === "no_invoice") {
            return label === "No Payment" ? "text-bg-danger" : "text-bg-secondary";
        }
        return {
            grace_period: "text-bg-info",
            order_pick: "text-bg-warning",
            ship: "text-bg-success",
        }[stage] || "text-bg-secondary";
    },

    // The server already returns props.docs.lines in clearance-priority
    // order (see stock_forecasted.py's _get_report_data) — that's the
    // default and needs no client-side work. Both other modes are purely
    // client-side re-sorts of the same, already-fetched lines, so
    // switching back and forth is instant and never re-fetches the report.
    get clearanceSortedLines() {
        const lines = this.props.docs.lines;
        const mode = this.clearanceSortState.mode;

        if (mode === "delivery") {
            return [...lines].sort((a, b) => {
                const dateA = a.move_out && a.move_out.date;
                const dateB = b.move_out && b.move_out.date;
                if (!dateA && !dateB) return 0;
                if (!dateA) return 1;
                if (!dateB) return -1;
                return dateA < dateB ? -1 : dateA > dateB ? 1 : 0;
            });
        }

        if (mode === "locked") {
            // Locked lines float to the top, earliest lock first; every
            // other line keeps its existing relative order (the server's
            // own clearance-priority sort) below them.
            const locked = lines.filter((l) => this._isLockedLine(l));
            const rest = lines.filter((l) => !this._isLockedLine(l));
            locked.sort((a, b) => {
                const dateA = a.lock_date;
                const dateB = b.lock_date;
                if (!dateA && !dateB) return 0;
                if (!dateA) return 1;
                if (!dateB) return -1;
                return dateA < dateB ? -1 : dateA > dateB ? 1 : 0;
            });
            return [...locked, ...rest];
        }

        return lines;
    },
});
