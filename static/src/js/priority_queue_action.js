/** @odoo-module **/

import { Component, onMounted, xml } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const ORDER_BY = {
    score: [
        { name: "x_ai_priority_score", asc: false },
        { name: "x_ai_sla_deadline", asc: true },
        { name: "create_date", asc: true },
        { name: "id", asc: true },
    ],
    manual: [
        { name: "x_ai_manual_override", asc: false },
        { name: "x_manual_priority_rank_display", asc: true },
        { name: "x_ai_priority_score", asc: false },
        { name: "x_ai_sla_deadline", asc: true },
        { name: "create_date", asc: true },
        { name: "id", asc: true },
    ],
};

class PriorityQueueAction extends Component {
    static template = xml`<div class="o_priority_queue_action d-none"/>`;

    setup() {
        this.actionService = useService("action");
        onMounted(() => this._openQueue());
    }

    async _openQueue() {
        const params = this.props.action.params || {};
        const queueMode = params.queue_mode || "score";
        const orderBy = ORDER_BY[queueMode] || ORDER_BY.score;
        const windowAction = params.window_action_xmlid || "odoo_picking_priority.action_picking_tree_priority_score";
        await this.actionService.doAction(windowAction, {
            clearBreadcrumbs: true,
            props: { orderBy },
        });
    }
}

registry.category("actions").add("odoo_picking_priority_open_priority_queue", PriorityQueueAction);
