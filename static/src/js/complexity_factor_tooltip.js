/** @odoo-module **/

import { Component, xml } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

const FACTOR_LABELS = {
    x_ai_factor_sla: "SLA",
    x_ai_factor_availability: "Availability",
    x_ai_factor_urgency: "Urgency",
    x_ai_factor_channel: "Channel",
    x_ai_factor_dependency: "Dependency",
    x_ai_factor_value: "Value",
    x_ai_factor_complexity: "Complexity",
    x_ai_complexity_debug_score: "Complexity",
};

const FACTOR_KEYS = {
    x_ai_factor_sla: "sla",
    x_ai_factor_availability: "availability",
    x_ai_factor_urgency: "urgency",
    x_ai_factor_channel: "channel",
    x_ai_factor_dependency: "dependency",
    x_ai_factor_value: "value",
    x_ai_factor_complexity: "complexity",
    x_ai_complexity_debug_score: "complexity",
};

export class PriorityFactorTooltipField extends Component {
    static template = xml/* html */ `
        <span class="o_priority_factor_tooltip d-inline-flex align-items-center gap-1"
              t-att-title="tooltipText"
              style="cursor: help;">
            <span t-esc="formattedValue"/>
            <i class="fa fa-circle-info text-muted" aria-hidden="true"/>
        </span>`;

    static props = {
        ...standardFieldProps,
    };

    static supportedTypes = ["float"];

    get formattedValue() {
        const value = Number(this.props.value ?? this.props.record.data[this.props.name] ?? 0);
        return value.toFixed(2);
    }

    get factorKey() {
        return FACTOR_KEYS[this.props.name] || this.props.name || "factor";
    }

    get factorLabel() {
        return FACTOR_LABELS[this.props.name] || this.props.string || this.props.name || "Factor";
    }

    get reasonData() {
        const raw = this.props.record.data.x_ai_priority_reason_json;
        if (!raw) {
            return {};
        }
        try {
            return JSON.parse(raw);
        } catch {
            return {};
        }
    }

    get factorReason() {
        const parsed = this.reasonData;
        const factorValues = parsed.factors || {};
        const reasons = Array.isArray(parsed.reasons) ? parsed.reasons : [];
        const reasonLine = reasons.find((entry) => entry.factor === this.factorKey);
        return {
            score: factorValues[this.factorKey],
            label: reasonLine?.label || false,
        };
    }

    get tooltipText() {
        const lines = [`${this.factorLabel}`, `Score: ${this.formattedValue}`];
        const reason = this.factorReason;
        if (reason.label) {
            lines.push(`Reason: ${reason.label}`);
        }

        if (this.factorKey === "complexity") {
            const summary =
                this.props.record.data.x_ai_complexity_debug_summary ||
                "Complexity is based on distinct product count and source zone count.";
            const details = this.props.record.data.x_ai_complexity_debug_details;
            lines.push("", summary);
            if (details) {
                lines.push("", "Breakdown:", details);
            }
        }

        return lines.join("\n");
    }
}

registry.category("fields").add("priority_factor_tooltip", {
    component: PriorityFactorTooltipField,
});

registry.category("fields").add("complexity_factor_tooltip", {
    component: PriorityFactorTooltipField,
});
