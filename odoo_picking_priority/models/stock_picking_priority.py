import json
import logging
from datetime import datetime, timedelta

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from .http_json import JsonHttpRequestError, post_json

_logger = logging.getLogger(__name__)

# The picking priority engine is built around one fixed scoring budget.
# Every factor is normalized into a 0-100 scale so the final rank is easy to
# compare across companies, warehouses, and scoring providers.
PRIORITY_VERSION = "wms-priority-v1.0"
MAX_PRIORITY_SCORE = 100.0
DEFAULT_OPEN_PICKING_STATES = ("confirmed", "assigned")
# Default factor weights are used both as the fallback scoring policy and as
# the baseline when the user edits a policy draft through the wizard.
DEFAULT_FACTOR_CONFIG = {
    "sla": {"max": 30.0, "enabled": True},
    "availability": {"max": 20.0, "enabled": True},
    "urgency": {"max": 15.0, "enabled": True},
    "channel": {"max": 10.0, "enabled": True},
    "dependency": {"max": 10.0, "enabled": True},
    "value": {"max": 10.0, "enabled": True},
    "complexity": {"max": 5.0, "enabled": True},
}
FACTOR_SELECTION = [
    ("sla", "SLA / Deadline"),
    ("availability", "Availability"),
    ("urgency", "Manual Urgency"),
    ("channel", "Channel / Customer SLA"),
    ("dependency", "Downstream Dependency"),
    ("value", "Order Value / Business Impact"),
    ("complexity", "Picking Complexity / Quick Win"),
]
FACTOR_LABELS = dict(FACTOR_SELECTION)


class WmsAiPriorityConfig(models.Model):
    # Stores the weight assigned to a single scoring factor for one company /
    # warehouse combination. These records are the deterministic runtime config
    # consumed by the picking scorer.
    _name = "wms.ai.priority.config"
    _description = "WMS Picking Priority Configuration"
    _order = "sequence, id"

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company, index=True)
    warehouse_id = fields.Many2one("stock.warehouse", index=True)
    factor_name = fields.Selection(FACTOR_SELECTION, required=True, index=True)
    weight_max_score = fields.Float(required=True, default=0.0)
    enabled = fields.Boolean(default=True)
    threshold_json = fields.Text(string="Threshold JSON")
    _sql_constraints = [
        (
            "wms_ai_priority_config_unique",
            "unique(company_id, warehouse_id, factor_name)",
            "Priority configuration must be unique per company, warehouse, and factor.",
        ),
    ]

    @api.model
    def _audit_json(self, value):
        # Audit logs store JSON as text so we normalize dict/list values before
        # writing them. Existing strings are passed through unchanged.
        if value in (None, False):
            return False
        if isinstance(value, str):
            return value
        return json.dumps(value, indent=2, default=str)

    @api.model
    def _log_audit_event(self, action_type, action_message, **kwargs):
        # Audit logging should never break config writes, so failures are
        # swallowed after being logged server-side.
        try:
            return self.env["wms.ai.priority.log"].sudo().create_audit_log(
                action_type=action_type,
                action_message=action_message,
                **kwargs,
            )
        except Exception:
            _logger.exception("Failed to write audit log for %s", action_type)
            return False

    def _build_config_snapshot(self):
        # Capture a compact before/after snapshot so create/write/unlink events
        # are easy to inspect later from the audit table.
        self.ensure_one()
        return {
            "id": self.id,
            "name": self.name,
            "sequence": self.sequence,
            "active": self.active,
            "company_id": self.company_id.id if self.company_id else False,
            "warehouse_id": self.warehouse_id.id if self.warehouse_id else False,
            "factor_name": self.factor_name,
            "weight_max_score": self.weight_max_score,
            "enabled": self.enabled,
            "threshold_json": self.threshold_json,
        }

    @api.model_create_multi
    def create(self, vals_list):
        # Every config mutation is logged with the exact payload that created it
        # plus a normalized record snapshot for later review.
        records = super().create(vals_list)
        for record, vals in zip(records, vals_list):
            record._log_audit_event(
                "config_created",
                "Created priority config for %s" % (record.name or record.factor_name),
                company_id=record.company_id.id,
                reason_json=record._audit_json(
                    {
                        "change_type": "create",
                        "new_values": vals,
                        "record": record._build_config_snapshot(),
                    }
                ),
            )
        return records

    def write(self, vals):
        # We capture the previous state first so the audit record can show a
        # human-readable diff between the old and new config values.
        before_snapshots = {record.id: record._build_config_snapshot() for record in self}
        result = super().write(vals)
        for record in self:
            record._log_audit_event(
                "config_updated",
                "Updated priority config for %s" % (record.name or record.factor_name),
                company_id=record.company_id.id,
                reason_json=record._audit_json(
                    {
                        "change_type": "write",
                        "before": before_snapshots.get(record.id),
                        "after": record._build_config_snapshot(),
                        "changed_values": vals,
                    }
                ),
            )
        return result

    def unlink(self):
        # Deletions are also audited because removing a factor can completely
        # change the scoring output for open pickings.
        snapshots = [record._build_config_snapshot() for record in self]
        company_ids = list({record.company_id.id for record in self if record.company_id})
        result = super().unlink()
        for snapshot in snapshots:
            self._log_audit_event(
                "config_deleted",
                "Deleted priority config for %s" % (snapshot.get("name") or snapshot.get("factor_name")),
                company_id=snapshot.get("company_id") or self.env.company.id,
                reason_json=self._audit_json(
                    {
                        "change_type": "unlink",
                        "deleted_record": snapshot,
                        "company_ids": company_ids,
                    }
                ),
            )
        return result


class WmsAiPriorityLog(models.Model):
    # Central append-only history for scoring, overrides, AI actions, and
    # policy changes. This table is the easiest place to diagnose "why did this
    # picking move?" after the fact.
    _name = "wms.ai.priority.log"
    _description = "WMS Picking Priority Audit Log"
    _order = "scored_at desc, id desc"

    def _auto_init(self):
        # Keep older databases compatible by adding the action_type column if
        # the module is installed on a schema that predates this field.
        result = super()._auto_init()
        self.env.cr.execute(
            f"""
                ALTER TABLE "{self._table}"
                ADD COLUMN IF NOT EXISTS "action_type" VARCHAR DEFAULT 'picking_recalculated'
            """
        )
        return result

    action_type = fields.Selection(
        [
            ("picking_recalculated", "Picking Recalculated"),
            ("picking_recalculated_manual", "Picking Recalculated Manually"),
            ("picking_recalculated_auto", "Picking Recalculated by Scheduler"),
            ("manual_override", "Manual Override"),
            ("manual_rank_edit", "Manual Rank Edited"),
            ("sla_edit", "SLA Edited"),
            ("urgency_edit", "Urgency Edited"),
            ("config_created", "Config Created"),
            ("config_updated", "Config Updated"),
            ("config_deleted", "Config Deleted"),
            ("policy_preview", "Policy Preview Generated"),
            ("policy_applied", "Policy Applied"),
            ("policy_reset", "Policy Reset"),
            ("ai_question", "AI Question Asked"),
            ("ai_queue_summary", "AI Queue Summary"),
            ("ai_connection_test", "AI Connection Test"),
            ("ai_configuration_saved", "AI Configuration Saved"),
            ("ai_simulation", "AI Simulation"),
            ("ai_search", "AI Search"),
            ("ai_config_saved", "AI Configuration Saved"),
            ("ai_config_test", "AI Configuration Test"),
            ("ai_action", "AI Action"),
        ],
        required=True,
        default="picking_recalculated",
    )
    picking_id = fields.Many2one("stock.picking", index=True, ondelete="set null")
    company_id = fields.Many2one("res.company", index=True, required=True, default=lambda self: self.env.company)
    action_user_id = fields.Many2one("res.users", string="Action By", required=True, default=lambda self: self.env.user)
    action_message = fields.Text(string="Action Message")
    score = fields.Float()
    rank = fields.Integer()
    final_human_rank = fields.Integer()
    bucket = fields.Selection(
        [("critical", "Critical"), ("high", "High"), ("medium", "Medium"), ("low", "Low")]
    )
    factor_sla = fields.Float()
    factor_availability = fields.Float()
    factor_urgency = fields.Float()
    factor_channel = fields.Float()
    factor_dependency = fields.Float()
    factor_value = fields.Float()
    factor_complexity = fields.Float()
    delay_risk = fields.Selection(
        [("critical", "Critical"), ("high", "High"), ("medium", "Medium"), ("low", "Low")]
    )
    delay_risk_reason = fields.Text()
    reason_json = fields.Text()
    recommendation_version = fields.Char()
    sla_deadline = fields.Datetime()
    sla_source = fields.Char()
    scored_at = fields.Datetime(default=fields.Datetime.now, required=True)
    overridden = fields.Boolean()
    override_user_id = fields.Many2one("res.users")
    override_reason = fields.Text()

    @api.model
    def create_audit_log(
        self,
        *,
        action_type,
        action_message,
        company_id=None,
        picking_id=None,
        score=0.0,
        rank=0,
        final_human_rank=0,
        bucket=False,
        sla_deadline=False,
        sla_source=False,
        overridden=False,
        override_user_id=False,
        override_reason=False,
        factor_values=None,
        delay_risk=False,
        delay_risk_reason=False,
        reason_json=False,
        recommendation_version=False,
    ):
        # This helper centralizes all audit writes so the calling code only has
        # to describe the event and optional score/factor details.
        values = {
            "action_type": action_type,
            "action_message": action_message,
            "company_id": company_id or self.env.company.id,
            "action_user_id": self.env.user.id,
            "picking_id": picking_id,
            "score": score,
            "rank": rank,
            "final_human_rank": final_human_rank,
            "bucket": bucket,
            "sla_deadline": sla_deadline,
            "sla_source": sla_source,
            "overridden": overridden,
            "override_user_id": override_user_id or False,
            "override_reason": override_reason or False,
            "delay_risk": delay_risk,
            "delay_risk_reason": delay_risk_reason,
            "reason_json": reason_json,
            "recommendation_version": recommendation_version,
        }
        if factor_values:
            values.update(factor_values)
        return self.create(values)


class WmsAiPriorityPopup(models.TransientModel):
    # Read-only popup that mirrors the computed picking fields for quick
    # inspection from the list view or form view.
    _name = "wms.ai.priority.popup"
    _description = "WMS Picking Priority Popup"

    picking_id = fields.Many2one("stock.picking", required=True, readonly=True)
    x_manual_priority_rank_display = fields.Integer(
        related="picking_id.x_manual_priority_rank_display",
        readonly=True,
    )
    x_display_priority_rank = fields.Integer(related="picking_id.x_display_priority_rank", readonly=True)
    x_ai_priority_score = fields.Float(related="picking_id.x_ai_priority_score", readonly=True)
    x_ai_priority_bucket = fields.Selection(related="picking_id.x_ai_priority_bucket", readonly=True)
    x_ai_priority_rank = fields.Integer(related="picking_id.x_ai_priority_rank", readonly=True)
    x_ai_delay_risk = fields.Selection(related="picking_id.x_ai_delay_risk", readonly=True)
    x_ai_recommended_action = fields.Selection(related="picking_id.x_ai_recommended_action", readonly=True)
    x_ai_priority_reason = fields.Text(related="picking_id.x_ai_priority_reason", readonly=True)
    x_ai_delay_risk_reason = fields.Text(related="picking_id.x_ai_delay_risk_reason", readonly=True)
    x_ai_last_scored_at = fields.Datetime(related="picking_id.x_ai_last_scored_at", readonly=True)
    x_ai_recommendation_version = fields.Char(related="picking_id.x_ai_recommendation_version", readonly=True)
    x_ai_total_demand_qty = fields.Float(related="picking_id.x_ai_total_demand_qty", readonly=True)
    x_ai_total_reserved_qty = fields.Float(related="picking_id.x_ai_total_reserved_qty", readonly=True)
    x_ai_availability_ratio = fields.Float(related="picking_id.x_ai_availability_ratio", readonly=True)
    x_ai_stock_gap_summary = fields.Text(related="picking_id.x_ai_stock_gap_summary", readonly=True)


class WmsAiPriorityPolicyPrompt(models.Model):
    # Wizard used to turn a natural-language policy draft into factor weights,
    # validate the result, and apply it to the live config records.
    _name = "wms.ai.priority.policy.prompt"
    _description = "WMS Priority Policy Prompt"
    _order = "write_date desc, id desc"

    name = fields.Char(required=True, default=lambda self: self._default_name())
    active = fields.Boolean(default=True)
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company, required=True, index=True)
    warehouse_id = fields.Many2one("stock.warehouse", index=True)
    prompt_input = fields.Text(required=True, string="Policy Prompt")
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("ready", "Ready for Approval"),
            ("applied", "Applied"),
            ("error", "Validation Failed"),
        ],
        default="draft",
        required=True,
    )
    proposal_json = fields.Text(string="Generated Proposal JSON", readonly=True)
    preview_summary = fields.Text(string="Policy Preview", readonly=True)
    validation_status = fields.Selection(
        [("pending", "Pending"), ("valid", "Valid"), ("invalid", "Invalid")],
        string="Validation",
        default="pending",
        readonly=True,
    )
    validation_message = fields.Text(readonly=True)
    apply_note = fields.Text(readonly=True)
    approved_by_id = fields.Many2one("res.users", string="Applied By", readonly=True)
    approved_at = fields.Datetime(string="Applied At", readonly=True)

    @api.model
    def _default_name(self):
        return "Policy Draft - %s" % fields.Datetime.now()

    @api.model
    def _log_audit_event(self, action_type, action_message, **kwargs):
        try:
            return self.env["wms.ai.priority.log"].sudo().create_audit_log(
                action_type=action_type,
                action_message=action_message,
                **kwargs,
            )
        except Exception:
            _logger.exception("Failed to write audit log for %s", action_type)
            return False

    @api.model
    def _audit_json(self, value):
        if value in (None, False):
            return False
        if isinstance(value, str):
            return value
        return json.dumps(value, indent=2, default=str)

    def action_generate_preview(self):
        # Build a proposal from the prompt, validate it, and store the preview
        # so the user can review the policy before it is applied to live data.
        for record in self:
            try:
                proposal = record._build_policy_proposal_from_prompt(record.prompt_input or "")
                is_valid, validation_message = record._validate_policy_proposal(proposal)
                record.write(
                    {
                        "proposal_json": json.dumps(proposal, indent=2),
                        "preview_summary": record._build_policy_preview_text(proposal),
                        "validation_status": "valid" if is_valid else "invalid",
                        "validation_message": validation_message,
                        "apply_note": (
                            "Applying this draft will update deterministic factor weights for the selected company"
                            " / warehouse. Suggested special rules remain stored as advisory preview notes in"
                            " Phase 1 and are not auto-executed."
                        ),
                        "state": "ready" if is_valid else "error",
                    }
                )
                record._log_audit_event(
                    "policy_preview",
                    "Generated policy preview for %s" % (record.name or "Policy Draft"),
                    company_id=record.company_id.id,
                    reason_json=record._audit_json(
                        {
                            "policy_name": record.name or "Policy Draft",
                            "warehouse_id": record.warehouse_id.id if record.warehouse_id else False,
                            "validation_status": "valid" if is_valid else "invalid",
                            "validation_message": validation_message,
                            "prompt_input": record.prompt_input,
                            "proposal": proposal,
                        }
                    ),
                )
            except Exception as error:
                record._log_audit_event(
                    "policy_preview",
                    "Policy preview failed for %s" % (record.name or "Policy Draft"),
                    company_id=record.company_id.id,
                    reason_json=record._audit_json(
                        {
                            "policy_name": record.name or "Policy Draft",
                            "warehouse_id": record.warehouse_id.id if record.warehouse_id else False,
                            "prompt_input": record.prompt_input,
                            "error": str(error),
                        }
                    ),
                )
                raise
        return True

    def action_reset_to_draft(self):
        # Reset clears the generated JSON and moves the wizard back to a clean
        # draft state so the user can rewrite the policy from scratch.
        self.write(
            {
                "state": "draft",
                "proposal_json": False,
                "preview_summary": False,
                "validation_status": "pending",
                "validation_message": False,
                "apply_note": False,
            }
        )
        self._log_audit_event(
            "policy_reset",
            "Reset policy draft %s to draft state" % (self.name or "Policy Draft"),
            company_id=self.company_id.id,
            reason_json=self._audit_json(
                {
                    "policy_name": self.name or "Policy Draft",
                    "company_id": self.company_id.id,
                    "warehouse_id": self.warehouse_id.id if self.warehouse_id else False,
                }
            ),
        )
        return True

    def action_apply_policy(self):
        # Applying the wizard writes one config record per supported factor,
        # replacing existing weights for the same company/warehouse/factor key.
        config_model = self.env["wms.ai.priority.config"]
        for record in self:
            try:
                proposal = record._get_proposal_dict()
                is_valid, validation_message = record._validate_policy_proposal(proposal)
                if not is_valid:
                    raise ValidationError(validation_message)

                weights = proposal["weights_by_context"]["default"]
                for sequence, factor_name in enumerate(DEFAULT_FACTOR_CONFIG.keys(), start=1):
                    values = {
                        "name": FACTOR_LABELS[factor_name],
                        "sequence": sequence,
                        "active": True,
                        "company_id": record.company_id.id,
                        "warehouse_id": record.warehouse_id.id if record.warehouse_id else False,
                        "factor_name": factor_name,
                        "weight_max_score": weights[factor_name],
                        "enabled": weights[factor_name] > 0,
                    }
                    existing = config_model.search(
                        [
                            ("company_id", "=", record.company_id.id),
                            ("warehouse_id", "=", record.warehouse_id.id if record.warehouse_id else False),
                            ("factor_name", "=", factor_name),
                        ],
                        limit=1,
                    )
                    if existing:
                        existing.write(values)
                    else:
                        config_model.create(values)

                record.write(
                    {
                        "state": "applied",
                        "approved_by_id": self.env.user.id,
                        "approved_at": fields.Datetime.now(),
                        "validation_status": "valid",
                        "validation_message": validation_message,
                    }
                )
                record._log_audit_event(
                    "policy_applied",
                    "Applied policy %s" % (record.name or "Policy Draft"),
                    company_id=record.company_id.id,
                    reason_json=record._audit_json(
                        {
                            "policy_name": record.name or "Policy Draft",
                            "company_id": record.company_id.id,
                            "warehouse_id": record.warehouse_id.id if record.warehouse_id else False,
                            "proposal": proposal,
                            "validation_message": validation_message,
                        }
                    ),
                )
            except Exception as error:
                record._log_audit_event(
                    "policy_applied",
                    "Policy application failed for %s" % (record.name or "Policy Draft"),
                    company_id=record.company_id.id,
                    reason_json=record._audit_json(
                        {
                            "policy_name": record.name or "Policy Draft",
                            "company_id": record.company_id.id,
                            "warehouse_id": record.warehouse_id.id if record.warehouse_id else False,
                            "error": str(error),
                        }
                    ),
                )
                raise
        return True

    def _get_proposal_dict(self):
        # The apply step always uses the stored JSON preview so we are applying
        # exactly what the manager reviewed.
        self.ensure_one()
        if not self.proposal_json:
            raise ValidationError("Generate a policy preview before applying the draft.")
        try:
            return json.loads(self.proposal_json)
        except json.JSONDecodeError as error:
            raise ValidationError("Generated proposal JSON is invalid: %s" % error) from error

    def _build_policy_proposal_from_prompt(self, prompt_text):
        # Start from the default factor template and let the prompt tilt the
        # weights toward the business priority words that were mentioned.
        self.ensure_one()
        normalized_prompt = (prompt_text or "").strip().lower()
        weights = {
            factor_name: values["max"]
            for factor_name, values in DEFAULT_FACTOR_CONFIG.items()
        }
        notes = []
        special_rules = []

        def apply_adjustment(factor_name, delta, note):
            weights[factor_name] = max(weights[factor_name] + delta, 0.0)
            notes.append(note)

        if any(keyword in normalized_prompt for keyword in ("sla", "deadline", "dispatch", "cutoff", "cut-off")):
            apply_adjustment("sla", 10.0, "Prompt emphasizes dispatch deadline / SLA urgency.")
        if any(keyword in normalized_prompt for keyword in ("stock readiness", "stock ready", "availability", "reserved", "full availability", "readiness")):
            apply_adjustment("availability", 8.0, "Prompt emphasizes ready-to-ship availability.")
        if any(keyword in normalized_prompt for keyword in ("marketplace", "ecommerce", "e-commerce", "web order", "same-day")):
            apply_adjustment("channel", 5.0, "Prompt prioritizes marketplace or same-day channels.")
            apply_adjustment("sla", 5.0, "Marketplace handling increases SLA importance.")
            special_rules.append("If channel = marketplace and deadline < 4h, add +10 advisory boost")
        if any(keyword in normalized_prompt for keyword in ("b2b", "wholesale", "enterprise customer")):
            apply_adjustment("value", 4.0, "Prompt references B2B / wholesale business impact.")
            apply_adjustment("dependency", 2.0, "Prompt suggests downstream dependency for B2B deliveries.")
        if "value matters a little" in normalized_prompt or "value matters little" in normalized_prompt:
            weights["value"] = max(weights["value"] - 4.0, 0.0)
            notes.append("Prompt says order value should have a small influence.")
        elif any(keyword in normalized_prompt for keyword in ("order value", "high-value", "business value", "revenue")):
            apply_adjustment("value", 6.0, "Prompt raises order value / business impact.")
        if any(keyword in normalized_prompt for keyword in ("internal transfer", "internal transfers")):
            weights["channel"] = max(weights["channel"] - 3.0, 0.0)
            weights["dependency"] = max(weights["dependency"] - 2.0, 0.0)
            notes.append("Prompt keeps standard internal transfers below customer deliveries.")
            special_rules.append("If operation_type = internal and critical_flag = false, cap score at 60")
        if any(keyword in normalized_prompt for keyword in ("critical replenishment", "store replenishment", "replenishment critical")):
            apply_adjustment("dependency", 6.0, "Prompt elevates replenishment dependencies.")
            special_rules.append("If replenishment is critical, add +8 advisory boost")
        if any(keyword in normalized_prompt for keyword in ("month-end", "invoice-ready", "billing cycle", "invoice ready")):
            apply_adjustment("dependency", 5.0, "Prompt prioritizes invoice or month-end dependencies.")
            special_rules.append("If invoice-ready during month-end, add +6 advisory boost")
        if any(keyword in normalized_prompt for keyword in ("urgent", "manual urgency", "escalation", "critical flag")):
            apply_adjustment("urgency", 5.0, "Prompt values supervisor urgency input.")
        if any(keyword in normalized_prompt for keyword in ("quick win", "simple pick", "small order", "easy pick")):
            apply_adjustment("complexity", 3.0, "Prompt favors quick-win or simple pickings.")

        total_weight = sum(weights.values()) or 1.0
        normalized_weights = {
            factor_name: round((weight / total_weight) * MAX_PRIORITY_SCORE, 2)
            for factor_name, weight in weights.items()
        }
        rounding_gap = round(MAX_PRIORITY_SCORE - sum(normalized_weights.values()), 2)
        normalized_weights["sla"] = round(normalized_weights["sla"] + rounding_gap, 2)

        policy_name = self.name or "Prompt Draft"
        return {
            "policy_name": policy_name,
            "source_prompt": prompt_text,
            "weights_by_context": {"default": normalized_weights},
            "special_rules": special_rules,
            "notes": notes or ["Default rule-based scoring template retained with no strong adjustments detected."],
        }

    def _validate_policy_proposal(self, proposal):
        # Validation is intentionally strict: every supported factor must be
        # present, non-negative, and the total must still add up to 100.
        weights_by_context = proposal.get("weights_by_context") or {}
        default_weights = weights_by_context.get("default") or {}
        unsupported = sorted(set(default_weights) - set(DEFAULT_FACTOR_CONFIG))
        missing = [factor_name for factor_name in DEFAULT_FACTOR_CONFIG if factor_name not in default_weights]
        negative = [factor_name for factor_name, weight in default_weights.items() if weight < 0]
        total = round(sum(default_weights.values()), 2)

        issues = []
        if unsupported:
            issues.append("Unsupported factors: %s" % ", ".join(unsupported))
        if missing:
            issues.append("Missing factors: %s" % ", ".join(missing))
        if negative:
            issues.append("Negative weights found for: %s" % ", ".join(negative))
        if total != round(MAX_PRIORITY_SCORE, 2):
            issues.append("Weights must total 100. Current total: %.2f" % total)

        if issues:
            return False, "\n".join(issues)
        return True, "Proposal is valid and ready for manager approval."

    def _build_policy_preview_text(self, proposal):
        # The preview turns the JSON proposal into readable text so managers can
        # review the ranking emphasis without inspecting raw JSON.
        default_weights = proposal.get("weights_by_context", {}).get("default", {})
        ordered_weights = sorted(default_weights.items(), key=lambda item: item[1], reverse=True)
        lines = ["Policy Preview", "", "Top weights:"]
        for index, (factor_name, weight) in enumerate(ordered_weights, start=1):
            lines.append("%s. %s = %.2f" % (index, FACTOR_LABELS.get(factor_name, factor_name), weight))

        special_rules = proposal.get("special_rules") or []
        if special_rules:
            lines.extend(["", "Advisory special rules:"])
            for index, rule in enumerate(special_rules, start=1):
                lines.append("%s. %s" % (index, rule))

        notes = proposal.get("notes") or []
        if notes:
            lines.extend(["", "Interpretation notes:"])
            for index, note in enumerate(notes, start=1):
                lines.append("%s. %s" % (index, note))
        return "\n".join(lines)


class StockPicking(models.Model):
    # This extension is the heart of the module: it adds AI scoring fields,
    # manual override controls, deadline helpers, queue actions, and the actual
    # factor computation logic on top of stock.picking.
    _inherit = "stock.picking"

    x_ai_priority_score = fields.Float(
        string="Priority Score (%)",
        compute="_compute_ai_priority_fields",
        store=True,
        index=True,
    )
    x_ai_priority_bucket = fields.Selection(
        [("critical", "Critical"), ("high", "High"), ("medium", "Medium"), ("low", "Low")],
        string="Priority Bucket",
        compute="_compute_ai_priority_fields",
        store=True,
        index=True,
    )
    x_ai_priority_rank = fields.Integer(
        string="Priority Rank",
        compute="_compute_ai_priority_rank",
        store=True,
        index=True,
    )
    x_manual_priority_rank = fields.Integer(string="Manual Display Rank")
    x_manual_priority_rank_display = fields.Integer(
        string="Manual Rank",
        compute="_compute_manual_priority_rank_display",
        store=True,
        index=True,
    )
    x_display_priority_rank = fields.Integer(
        string="Final Rank",
        compute="_compute_display_priority_rank",
        store=True,
        index=True,
    )
    x_ai_priority_reason = fields.Text(
        string="Priority Explanation",
        compute="_compute_ai_priority_fields",
        store=True,
    )
    x_ai_priority_reason_json = fields.Text(
        string="Priority Reason JSON",
        compute="_compute_ai_priority_fields",
        store=True,
    )
    x_ai_delay_risk = fields.Selection(
        [("critical", "Critical"), ("high", "High"), ("medium", "Medium"), ("low", "Low")],
        string="Delay Risk",
        compute="_compute_ai_priority_fields",
        store=True,
        index=True,
    )
    x_ai_delay_risk_reason = fields.Text(
        string="Delay Risk Reason",
        compute="_compute_ai_priority_fields",
        store=True,
    )
    x_ai_recommended_action = fields.Selection(
        [
            ("pick_now", "Pick Now"),
            ("pick_next", "Pick Next"),
            ("expedite_stock", "Expedite Stock"),
            ("pick_available_and_replenish", "Pick Available + Replenish Missing"),
            ("review_override", "Review Override"),
            ("monitor", "Monitor"),
        ],
        string="Recommended Action",
        compute="_compute_ai_priority_fields",
        store=True,
    )
    x_ai_last_scored_at = fields.Datetime(
        string="Last Scored At",
        compute="_compute_ai_priority_fields",
        store=True,
    )
    x_ai_recommendation_version = fields.Char(
        string="Recommendation Version",
        compute="_compute_ai_priority_fields",
        store=True,
    )
    x_ai_manual_override = fields.Boolean(string="Priority Overridden", default=False)
    x_ai_override_reason = fields.Text(string="Override Reason")
    x_ai_override_user_id = fields.Many2one("res.users", string="Override By")
    x_ai_override_datetime = fields.Datetime(string="Override Time")
    x_ai_sla_deadline = fields.Datetime(string="SLA Deadline")
    x_ai_sla_manual = fields.Boolean(
        string="SLA Deadline Set Manually",
        default=False,
        copy=False,
    )
    x_ai_dispatch_cutoff = fields.Datetime(string="Dispatch Cutoff")
    x_ai_customer_sla_date = fields.Date(
        string="Customer SLA",
        compute="_compute_customer_sla_date",
        store=False,
        readonly=True,
    )
    x_ai_priority_tiebreaker = fields.Datetime(
        string="Priority Tie Breaker",
        compute="_compute_ai_priority_tiebreaker",
        store=True,
    )
    x_effective_priority_deadline = fields.Datetime(
        string="Effective Priority Deadline",
        compute="_compute_effective_priority_deadline",
        store=True,
    )
    x_ai_urgency_level = fields.Selection(
        [("normal", "Normal"), ("high", "High"), ("critical", "Critical")],
        string="Urgency Level",
        default="normal",
        required=True,
    )
    x_ai_urgency_manual = fields.Boolean(
        string="Urgency Set Manually",
        default=False,
        copy=False,
    )
    x_ai_source_channel = fields.Selection(
        [
            ("marketplace", "Marketplace"),
            ("retail_store", "Retail Store"),
            ("b2b", "B2B"),
            ("internal_transfer", "Internal Transfer"),
            ("store_replenishment", "Store Replenishment"),
            ("other", "Other"),
        ],
        string="Source Channel",
        default="other",
    )
    x_ai_factor_sla = fields.Float(string="Factor: SLA", compute="_compute_ai_priority_fields", store=True)
    x_ai_factor_availability = fields.Float(
        string="Factor: Availability",
        compute="_compute_ai_priority_fields",
        store=True,
    )
    x_ai_total_demand_qty = fields.Float(
        string="Total Demand Qty",
        compute="_compute_ai_priority_fields",
        store=True,
    )
    x_ai_total_reserved_qty = fields.Float(
        string="Total Reserved Qty",
        compute="_compute_ai_priority_fields",
        store=True,
    )
    x_ai_availability_ratio = fields.Float(
        string="Availability Ratio (%)",
        compute="_compute_ai_priority_fields",
        store=True,
    )
    x_ai_stock_gap_summary = fields.Text(
        string="Stock Gap Summary",
        compute="_compute_ai_priority_fields",
        store=True,
    )
    x_ai_factor_urgency = fields.Float(
        string="Factor: Urgency",
        compute="_compute_ai_priority_fields",
        store=True,
    )
    x_ai_factor_channel = fields.Float(
        string="Factor: Channel",
        compute="_compute_ai_priority_fields",
        store=True,
    )
    x_ai_factor_dependency = fields.Float(
        string="Factor: Dependency",
        compute="_compute_ai_priority_fields",
        store=True,
    )
    x_ai_factor_value = fields.Float(
        string="Factor: Value",
        compute="_compute_ai_priority_fields",
        store=True,
    )
    x_ai_factor_complexity = fields.Float(
        string="Factor: Complexity",
        compute="_compute_ai_priority_fields",
        store=True,
    )
    x_ai_complexity_product_count = fields.Integer(
        string="Complexity Product Count",
        compute="_compute_ai_complexity_debug",
        store=False,
        readonly=True,
    )
    x_ai_complexity_zone_count = fields.Integer(
        string="Complexity Zone Count",
        compute="_compute_ai_complexity_debug",
        store=False,
        readonly=True,
    )
    x_ai_complexity_debug_summary = fields.Char(
        string="Complexity Debug Summary",
        compute="_compute_ai_complexity_debug",
        store=False,
        readonly=True,
    )
    x_ai_complexity_debug_details = fields.Text(
        string="Complexity Debug Details",
        compute="_compute_ai_complexity_debug",
        store=False,
        readonly=True,
    )
    x_ai_complexity_debug_score = fields.Float(
        string="Complexity Debug Score",
        compute="_compute_ai_complexity_debug",
        store=False,
        readonly=True,
    )
    x_ai_log_ids = fields.One2many("wms.ai.priority.log", "picking_id", string="Priority Logs")

    # Compatibility aliases for the initial MVP field names.
    priority_score = fields.Float(
        string="Priority Score (%)",
        related="x_ai_priority_score",
        store=True,
        readonly=True,
    )
    priority_rank = fields.Integer(string="Priority Rank", related="x_ai_priority_rank", readonly=True)
    priority_explanation = fields.Text(
        string="Priority Reason",
        related="x_ai_priority_reason",
        readonly=True,
    )

    def write(self, vals):
        # Manual changes to urgency, SLA, and override fields are tracked so we
        # can keep the AI-derived values in sync with the user's intent.
        vals = dict(vals)
        skip_urgency_manual = self.env.context.get("skip_ai_urgency_manual")
        skip_sla_manual = self.env.context.get("skip_ai_sla_manual")
        explicit_urgency_change = "x_ai_urgency_level" in vals
        explicit_sla_change = "x_ai_sla_deadline" in vals
        explicit_manual_rank_change = "x_manual_priority_rank" in vals
        explicit_manual_override_change = "x_ai_manual_override" in vals
        deadline_fields_changed = any(
            field_name in vals for field_name in ("x_ai_sla_deadline", "x_ai_dispatch_cutoff", "date_deadline", "scheduled_date")
        )
        sla_source_fields_changed = any(field_name in vals for field_name in ("sale_id", "origin"))
        if vals.get("x_ai_manual_override"):
            vals.setdefault("x_ai_override_user_id", self.env.user.id)
            vals.setdefault("x_ai_override_datetime", fields.Datetime.now())
        elif vals.get("x_ai_manual_override") is False:
            vals.setdefault("x_manual_priority_rank", False)
            vals.setdefault("x_ai_override_reason", False)
            vals.setdefault("x_ai_override_user_id", False)
            vals.setdefault("x_ai_override_datetime", False)
        elif "x_ai_override_reason" in vals and any(self.mapped("x_ai_manual_override")):
            vals.setdefault("x_ai_override_user_id", self.env.user.id)
            vals.setdefault("x_ai_override_datetime", fields.Datetime.now())
        if explicit_urgency_change and not skip_urgency_manual:
            vals["x_ai_urgency_manual"] = bool(vals.get("x_ai_urgency_level"))
        if explicit_sla_change and not skip_sla_manual:
            vals["x_ai_sla_manual"] = bool(vals.get("x_ai_sla_deadline"))
        if deadline_fields_changed and not explicit_urgency_change and not skip_urgency_manual:
            vals["x_ai_urgency_manual"] = False
        result = super().write(vals)
        if (deadline_fields_changed or sla_source_fields_changed) and not explicit_sla_change and not skip_sla_manual:
            self.filtered(lambda picking: not picking.x_ai_sla_manual)._autofill_ai_sla_deadline()
        if deadline_fields_changed and not explicit_urgency_change and not skip_urgency_manual:
            self._autofill_ai_urgency_level()
        if explicit_sla_change:
            self._create_ai_priority_logs(action_type="sla_edit", action_message="Updated SLA deadline")
        if explicit_urgency_change:
            self._create_ai_priority_logs(action_type="urgency_edit", action_message="Updated urgency level")
        if explicit_manual_rank_change and not (explicit_manual_override_change and vals.get("x_ai_manual_override") is False):
            self._create_ai_priority_logs(action_type="manual_rank_edit", action_message="Updated manual priority rank")
        return result

    @api.model_create_multi
    def create(self, vals_list):
        # New pickings are given automatic SLA/urgency values unless the caller
        # explicitly marks them as manually controlled.
        prepared_vals_list = []
        for vals in vals_list:
            prepared_vals = dict(vals)
            prepared_vals.setdefault("x_ai_sla_manual", False)
            if prepared_vals.get("x_ai_urgency_level"):
                prepared_vals.setdefault("x_ai_urgency_manual", True)
            prepared_vals_list.append(prepared_vals)
        records = super().create(prepared_vals_list)
        records.filtered(lambda picking: not picking.x_ai_sla_manual)._autofill_ai_sla_deadline()
        records.filtered(lambda picking: not picking.x_ai_urgency_manual)._autofill_ai_urgency_level()
        return records

    @api.onchange("sale_id")
    def _onchange_autofill_ai_sla_deadline(self):
        # In the form view, changing the sale order should immediately surface
        # the best-effort SLA deadline unless the user has locked it manually.
        for picking in self:
            if not picking.x_ai_sla_manual:
                auto_sla = picking._get_auto_ai_sla_deadline()
                if auto_sla:
                    picking.x_ai_sla_deadline = auto_sla

    @api.onchange("x_ai_sla_deadline", "x_ai_dispatch_cutoff", "date_deadline", "scheduled_date")
    def _onchange_autofill_ai_urgency_level(self):
        # Urgency is derived from the nearest meaningful deadline unless the
        # user has chosen to manage urgency by hand.
        for picking in self:
            if not picking.x_ai_urgency_manual:
                picking.x_ai_urgency_level = picking._get_auto_ai_urgency_level()

    def _autofill_ai_sla_deadline(self):
        # Server-side version of the onchange helper. This keeps stored records
        # consistent even when they are updated outside the UI.
        for picking in self:
            if picking.x_ai_sla_manual:
                continue
            auto_sla = picking._get_auto_ai_sla_deadline()
            if auto_sla and fields.Datetime.to_datetime(picking.x_ai_sla_deadline) != fields.Datetime.to_datetime(auto_sla):
                picking.with_context(skip_ai_sla_manual=True).write(
                    {
                        "x_ai_sla_deadline": auto_sla,
                        "x_ai_sla_manual": False,
                    }
                )

    def _autofill_ai_urgency_level(self):
        # Same idea as the SLA autofill above, but for urgency.
        for picking in self:
            auto_urgency = picking._get_auto_ai_urgency_level()
            if picking.x_ai_urgency_level != auto_urgency or picking.x_ai_urgency_manual:
                picking.with_context(skip_ai_urgency_manual=True).write(
                    {
                        "x_ai_urgency_level": auto_urgency,
                        "x_ai_urgency_manual": False,
                    }
                )

    def _get_sale_lead_time_sla(self):
        # Derive the SLA from product lead times on the linked sale order.
        self.ensure_one()
        sale_order = self.sale_id
        if not sale_order:
            return False

        base_datetime = fields.Datetime.to_datetime(sale_order.date_order) or fields.Datetime.now()
        candidate_dates = []
        for line in sale_order.order_line:
            if line.display_type or line._is_delivery():
                continue
            candidate_dates.append(base_datetime + timedelta(days=line.product_id.sale_delay or 0.0))

        if not candidate_dates:
            return False
        if getattr(sale_order, "picking_policy", "direct") == "one":
            return max(candidate_dates)
        return min(candidate_dates)

    def _get_partner_customer_sla_deadline(self):
        # Some customers define their own SLA window, which takes precedence
        # over product lead time if it is available.
        self.ensure_one()
        partner = self.partner_id.commercial_partner_id if self.partner_id and self.partner_id.commercial_partner_id else False
        if not partner or not partner.x_customer_sla_days:
            return False
        sale_order = self.sale_id
        base_datetime = fields.Datetime.to_datetime(sale_order.date_order) if sale_order and sale_order.date_order else fields.Datetime.now()
        return base_datetime + timedelta(days=partner.x_customer_sla_days)

    def _get_auto_ai_sla_deadline(self):
        # SLA resolution order: customer SLA first, then lead time, then the
        # sale order commitment date as a last-resort fallback.
        self.ensure_one()
        partner_customer_sla = self._get_partner_customer_sla_deadline()
        if partner_customer_sla:
            return fields.Datetime.to_datetime(partner_customer_sla)
        lead_time_sla = self._get_sale_lead_time_sla()
        if lead_time_sla:
            return lead_time_sla
        sale_order = self.sale_id
        commitment_date = getattr(sale_order, "commitment_date", False) if sale_order else False
        return commitment_date or False

    def _get_auto_ai_urgency_level(self):
        # Urgency is a simple time-to-deadline interpretation so humans can see
        # how close the order is to becoming critical.
        self.ensure_one()
        deadline = self._get_ai_operational_deadline() or self._get_ai_scoring_deadline()
        if not deadline:
            return "normal"
        hours_remaining = (fields.Datetime.to_datetime(deadline) - fields.Datetime.now()).total_seconds() / 3600.0
        if hours_remaining <= 2:
            return "critical"
        if hours_remaining <= 8:
            return "high"
        return "normal"

    @api.depends(
        "state",
        "scheduled_date",
        "date_deadline",
        "priority",
        "partner_id",
        "partner_id.commercial_partner_id",
        "move_ids.state",
        "move_ids.product_uom_qty",
        "move_ids.quantity",
        "move_ids.product_uom",
        "move_ids.product_id.lst_price",
        "move_ids.location_id",
        "move_ids.sale_line_id.order_id.amount_total",
        "sale_id",
        "sale_id.amount_total",
        "sale_id.invoice_status",
        "sale_id.expected_date",
        "partner_id.commercial_partner_id.x_customer_sla_days",
        "x_ai_sla_deadline",
        "x_ai_dispatch_cutoff",
        "x_ai_urgency_level",
        "x_ai_source_channel",
    )
    def _compute_ai_priority_fields(self):
        # This is the main compute hook for the AI score. It groups pickings by
        # company, optionally delegates to an external scorer, and always falls
        # back to the local deterministic model.
        company_max_order_value = self._get_company_max_order_values()
        for company in self.mapped("company_id"):
            company_pickings = self.filtered(lambda picking: picking.company_id == company)
            remaining_pickings = company_pickings
            if company.x_ai_use_external_scoring and company.x_ai_scoring_endpoint:
                remaining_pickings = self._apply_external_scoring(company_pickings, company_max_order_value)
            self._apply_local_scoring(remaining_pickings, company_max_order_value)

    @api.depends("x_ai_sla_deadline", "date_deadline", "scheduled_date")
    def _compute_effective_priority_deadline(self):
        # The effective deadline is the deadline actually used for ranking and
        # risk evaluation, regardless of which field supplied it.
        for picking in self:
            picking.x_effective_priority_deadline = picking._get_ai_scoring_deadline()

    @api.depends("x_ai_sla_deadline", "create_date")
    def _compute_ai_priority_tiebreaker(self):
        # If two pickings look similar, the SLA deadline or creation time acts as
        # a stable tiebreaker for sort order.
        for picking in self:
            picking.x_ai_priority_tiebreaker = picking.x_ai_sla_deadline or picking.create_date

    @api.depends(
        "partner_id",
        "partner_id.commercial_partner_id",
        "partner_id.commercial_partner_id.x_customer_sla_days",
        "sale_id",
        "sale_id.date_order",
        "sale_id.picking_policy",
        "sale_id.order_line",
        "sale_id.order_line.display_type",
        "sale_id.order_line.product_id",
        "sale_id.order_line.product_id.sale_delay",
        "sale_id.order_line.customer_lead",
    )
    def _compute_customer_sla_date(self):
        # This is a display-only convenience date for the UI.
        for picking in self:
            resolved_sla = picking._get_partner_customer_sla_deadline() or picking._get_sale_lead_time_sla()
            picking.x_ai_customer_sla_date = fields.Date.to_date(resolved_sla) if resolved_sla else False

    def _apply_local_scoring(self, pickings, company_max_order_value):
        # Local scoring is pure Odoo logic: compute factors, derive the bucket
        # and delay risk, then persist everything in one assignment step.
        for picking in pickings:
            factors, reason_lines = picking._get_ai_priority_factors(company_max_order_value)
            total = min(sum(factors.values()), MAX_PRIORITY_SCORE)
            delay_risk, delay_risk_reason = picking._get_ai_delay_risk(total, factors)
            total_demand, total_reserved, availability_ratio = picking._get_ai_availability_metrics()
            recommended_action = picking._get_ai_recommended_action(delay_risk, factors, total_demand, total_reserved)
            stock_gap_summary = picking._get_ai_stock_gap_summary()
            picking._assign_ai_scoring_values(
                total,
                factors,
                reason_lines,
                delay_risk,
                delay_risk_reason,
                recommended_action,
                total_demand,
                total_reserved,
                availability_ratio,
                stock_gap_summary,
                PRIORITY_VERSION,
            )

    def _apply_external_scoring(self, pickings, company_max_order_value):
        # If the company has an external scoring endpoint, we send a normalized
        # payload and only fall back to local scoring when the service fails.
        company = pickings[:1].company_id
        payload = self._build_external_scoring_payload(pickings, company_max_order_value)
        headers = {"Content-Type": "application/json"}
        if company.x_ai_scoring_token:
            headers["Authorization"] = "Bearer %s" % company.x_ai_scoring_token

        try:
            data = post_json(
                company.x_ai_scoring_endpoint,
                payload,
                headers=headers,
                timeout=company.x_ai_scoring_timeout or 5,
            )
            results = data.get("results", [])
        except JsonHttpRequestError as error:
            _logger.warning(
                "External priority scoring failed for company %s. Falling back to local scoring. Error: %s",
                company.display_name,
                error,
            )
            return pickings

        result_map = {result.get("picking_id"): result for result in results if result.get("picking_id")}
        applied_pickings = self.env["stock.picking"]
        for picking in pickings:
            result = result_map.get(picking.id)
            if not result:
                continue
            factors = result.get("factor_scores", {})
            reason_lines = [
                {"factor": "external", "score": 0.0, "label": reason}
                for reason in result.get("reason_summary", [])
            ]
            total_demand, total_reserved, availability_ratio = picking._get_ai_availability_metrics()
            delay_risk = result.get("delay_risk") or picking._get_ai_priority_bucket(result.get("priority_score", 0.0))
            delay_risk_reason = result.get("delay_risk_reason") or result.get("explanation_text") or "External scoring response did not provide a delay risk explanation."
            recommended_action = result.get("recommended_action") or picking._get_ai_recommended_action(
                delay_risk,
                {
                    "availability": factors.get("availability", 0.0),
                    "urgency": factors.get("urgency", 0.0),
                    "dependency": factors.get("dependency", 0.0),
                },
                total_demand,
                total_reserved,
            )
            stock_gap_summary = result.get("stock_gap_summary") or picking._get_ai_stock_gap_summary()
            picking._assign_ai_scoring_values(
                min(result.get("priority_score", 0.0), MAX_PRIORITY_SCORE),
                {
                    "sla": factors.get("sla", 0.0),
                    "availability": factors.get("availability", 0.0),
                    "urgency": factors.get("urgency", 0.0),
                    "channel": factors.get("channel", 0.0),
                    "dependency": factors.get("dependency", 0.0),
                    "value": factors.get("value", 0.0),
                    "complexity": factors.get("complexity", 0.0),
                },
                reason_lines or [{"factor": "external", "score": 0.0, "label": result.get("explanation_text", "External scoring applied")}],
                delay_risk,
                delay_risk_reason,
                recommended_action,
                total_demand,
                total_reserved,
                availability_ratio,
                stock_gap_summary,
                result.get("version", PRIORITY_VERSION),
                result.get("priority_bucket"),
            )
            applied_pickings |= picking
        return pickings - applied_pickings

    def _assign_ai_scoring_values(
        self,
        total,
        factors,
        reason_lines,
        delay_risk,
        delay_risk_reason,
        recommended_action,
        total_demand,
        total_reserved,
        availability_ratio,
        stock_gap_summary,
        recommendation_version,
        bucket=None,
    ):
        # Persist the complete scoring snapshot, including the explanation text
        # and a JSON trace of the factors that produced the score.
        self.ensure_one()
        resolved_bucket = bucket or self._get_ai_priority_bucket(total)
        self.x_ai_priority_score = total
        self.x_ai_priority_bucket = resolved_bucket
        self.x_ai_priority_reason = self._build_ai_reason_text(total, reason_lines, delay_risk_reason)
        self.x_ai_delay_risk = delay_risk
        self.x_ai_delay_risk_reason = delay_risk_reason
        self.x_ai_recommended_action = recommended_action
        self.x_ai_priority_reason_json = json.dumps(
            {
                "version": recommendation_version,
                "score": total,
                "bucket": resolved_bucket,
                "delay_risk": delay_risk,
                "delay_risk_reason": delay_risk_reason,
                "recommended_action": recommended_action,
                "reasons": reason_lines,
                "factors": factors,
            }
        )
        self.x_ai_last_scored_at = fields.Datetime.now()
        self.x_ai_recommendation_version = recommendation_version
        self.x_ai_factor_sla = factors["sla"]
        self.x_ai_factor_availability = factors["availability"]
        self.x_ai_total_demand_qty = total_demand
        self.x_ai_total_reserved_qty = total_reserved
        self.x_ai_availability_ratio = availability_ratio * 100
        self.x_ai_stock_gap_summary = stock_gap_summary
        self.x_ai_factor_urgency = factors["urgency"]
        self.x_ai_factor_channel = factors["channel"]
        self.x_ai_factor_dependency = factors["dependency"]
        self.x_ai_factor_value = factors["value"]
        self.x_ai_factor_complexity = factors["complexity"]

    def _build_external_scoring_payload(self, pickings, company_max_order_value):
        # External services receive only the data they need to reproduce the
        # same factor logic: deadlines, values, availability, source channel,
        # dependency type, and config maxima.
        company = pickings[:1].company_id
        max_order_value = company_max_order_value.get(company.id, 0.0)
        config = pickings[:1]._get_ai_factor_config() if pickings else DEFAULT_FACTOR_CONFIG
        payload_pickings = []
        for picking in pickings:
            total_demand, total_reserved, availability_ratio = picking._get_ai_availability_metrics()
            product_count, zone_count = picking._get_ai_complexity_metrics()
            deadline = picking._get_ai_scoring_deadline()
            hours_to_deadline = None
            if deadline:
                hours_to_deadline = (fields.Datetime.to_datetime(deadline) - fields.Datetime.now()).total_seconds() / 3600.0
            payload_pickings.append(
                {
                    "picking_id": picking.id,
                    "name": picking.name,
                    "warehouse_id": picking.picking_type_id.warehouse_id.id if picking.picking_type_id.warehouse_id else False,
                    "scheduled_date": picking.scheduled_date.isoformat() if picking.scheduled_date else False,
                    "sla_deadline": deadline.isoformat() if deadline else False,
                    "hours_to_deadline": hours_to_deadline,
                    "state": picking.state,
                    "urgency_level": picking.x_ai_urgency_level,
                    "source_channel": picking._get_effective_ai_source_channel(),
                    "order_value": picking._get_ai_order_value(),
                    "max_order_value_in_active_pool": max_order_value,
                    "total_demand_qty": total_demand,
                    "total_reserved_qty": total_reserved,
                    "availability_ratio": availability_ratio,
                    "product_count": product_count,
                    "zone_count": zone_count,
                    "dependency_type": picking._get_ai_dependency_type(),
                }
            )
        return {
            "company_id": company.id,
            "warehouse_id": pickings[:1].picking_type_id.warehouse_id.id if pickings[:1].picking_type_id.warehouse_id else False,
            "version": PRIORITY_VERSION,
            "pickings": payload_pickings,
            "config": {
                "%s_max" % factor_name: values["max"]
                for factor_name, values in config.items()
                if values.get("enabled")
            },
        }

    @api.depends("state", "x_ai_priority_score", "x_ai_sla_deadline", "create_date", "company_id")
    def _compute_ai_priority_rank(self):
        # Rank all open pickings within each company from highest score to
        # lowest, using the effective deadline as the secondary sort key.
        rank_map = {}
        for company in self.mapped("company_id"):
            open_states = self._get_ai_open_states(company)
            ranked_pickings = self.search(
                [
                    ("company_id", "=", company.id),
                    ("state", "in", open_states),
                ],
            )
            ranked_pickings._compute_effective_priority_deadline()
            ranked_pickings = ranked_pickings.sorted(
                key=lambda picking: (
                    -(picking.x_ai_priority_score or 0.0),
                    picking.x_ai_sla_deadline or picking.create_date or datetime.max,
                    picking.id,
                )
            )
            for rank, picking in enumerate(ranked_pickings, start=1):
                rank_map[picking.id] = rank

        for picking in self:
            open_states = picking._get_ai_open_states(picking.company_id)
            picking.x_ai_priority_rank = rank_map.get(picking.id, 0) if picking.state in open_states else 0

    @api.depends(
        "state",
        "x_ai_priority_rank",
        "x_ai_manual_override",
        "x_manual_priority_rank",
        "x_manual_priority_rank_display",
        "x_ai_priority_score",
        "x_ai_sla_deadline",
        "create_date",
    )
    def _compute_display_priority_rank(self):
        # Final display rank respects manual overrides before falling back to
        # the AI score.
        rank_map = {}
        for company in self.mapped("company_id"):
            open_states = self._get_ai_open_states(company)
            ranked_pickings = self.search(
                [
                    ("company_id", "=", company.id),
                    ("state", "in", open_states),
                ],
            )
            ranked_pickings._compute_effective_priority_deadline()
            ranked_pickings = ranked_pickings.sorted(
                key=lambda picking: (
                    -int(bool(picking.x_ai_manual_override)),
                    picking.x_manual_priority_rank_display or 0,
                    -(picking.x_ai_priority_score or 0.0),
                    picking.x_ai_sla_deadline or picking.create_date or datetime.max,
                    picking.id,
                )
            )
            for rank, picking in enumerate(ranked_pickings, start=1):
                rank_map[picking.id] = rank

        for picking in self:
            open_states = picking._get_ai_open_states(picking.company_id)
            picking.x_display_priority_rank = rank_map.get(picking.id, 0) if picking.state in open_states else 0

    @api.depends("x_ai_manual_override", "x_manual_priority_rank")
    def _compute_manual_priority_rank_display(self):
        # Manual rank is only visible when the picking is actually overridden.
        for picking in self:
            if picking.x_ai_manual_override and picking.x_manual_priority_rank:
                picking.x_manual_priority_rank_display = picking.x_manual_priority_rank
            else:
                picking.x_manual_priority_rank_display = 0

    def action_recalculate_ai_priority(self):
        # Recompute every open picking in the active companies from scratch.
        companies = self.mapped("company_id") or self.env.companies
        open_states = list(self._get_ai_open_states(companies[:1] if companies else self.env.company))
        pickings = self.search(
            [
                ("company_id", "in", companies.ids),
                ("state", "in", open_states),
            ]
        )
        return pickings._recalculate_ai_priority(
            "picking_recalculated_manual",
            "Manually recalculated picking priority",
        )

    def action_open_manual_rank_queue(self):
        # Open the queue view that emphasizes human-defined manual ranking.
        self._log_audit_event(
            "ai_action",
            "Opened manual rank queue",
            company_id=self[:1].company_id.id if self else self.env.company.id,
        )
        return self._get_priority_queue_action(
            "odoo_picking_priority.action_picking_tree_priority_manual",
            "odoo_picking_priority.view_picking_tree_priority_manual",
            "Deliveries",
        )

    def action_open_priority_score_queue(self):
        # Open the queue view sorted by the computed AI score.
        self._log_audit_event(
            "ai_action",
            "Opened priority score queue",
            company_id=self[:1].company_id.id if self else self.env.company.id,
        )
        return self._get_priority_queue_action(
            "odoo_picking_priority.action_picking_tree_priority_score",
            "odoo_picking_priority.view_picking_tree_priority_score",
            "Deliveries",
        )

    def _get_priority_queue_action(self, action_xmlid, list_view_xmlid, action_name):
        # Build the custom client action used by both queue entry points.
        context = dict(self.env.context or {})
        context.pop("orderedBy", None)
        context.setdefault("contact_display", "partner_address")
        context.setdefault("search_default_delivery", 1)
        context.setdefault("restricted_picking_type_code", "outgoing")
        company_ids = self.env.context.get("allowed_company_ids") or self.env.companies.ids
        if company_ids:
            context.setdefault("default_company_id", company_ids[0])
        return {
            "type": "ir.actions.client",
            "tag": "odoo_picking_priority_open_priority_queue",
            "name": action_name,
            "display_name": action_name,
            "params": {
                "queue_mode": "manual" if action_xmlid.endswith("_manual") else "score",
                "window_action_xmlid": action_xmlid,
                "list_view_xmlid": list_view_xmlid,
            },
            "context": context,
        }

    @api.model
    def cron_recalculate_ai_priority(self):
        # Scheduled job that keeps all active open pickings fresh without user
        # intervention.
        pickings = self.search([("state", "in", ["confirmed", "assigned", "waiting"])])
        pickings._recalculate_ai_priority(
            "picking_recalculated_auto",
            "Scheduler recalculated picking priority",
        )
        return True

    def action_mark_ai_override(self):
        # Mark the picking as manually overridden and preserve the current rank
        # as the starting point for the user's manual adjustment.
        now = fields.Datetime.now()
        for picking in self:
            picking.write(
                {
                    "x_ai_manual_override": True,
                    "x_manual_priority_rank": picking.x_manual_priority_rank or picking.x_display_priority_rank or picking.x_ai_priority_rank,
                    "x_ai_override_user_id": self.env.user.id,
                    "x_ai_override_datetime": now,
                }
            )
            picking._compute_display_priority_rank()
            picking._compute_manual_priority_rank_display()
            picking._create_ai_priority_logs(
                action_type="manual_override",
                action_message="Applied manual priority override",
            )
        return True

    def action_clear_ai_override(self):
        # Clearing the override returns the picking to fully AI-managed ranking.
        for picking in self:
            picking.write(
                {
                    "x_ai_manual_override": False,
                    "x_manual_priority_rank": False,
                    "x_ai_override_reason": False,
                    "x_ai_override_user_id": False,
                    "x_ai_override_datetime": False,
                }
            )
            picking._compute_display_priority_rank()
            picking._compute_manual_priority_rank_display()
            picking._create_ai_priority_logs(
                action_type="manual_override",
                action_message="Cleared manual priority override",
            )
        return True

    def action_view_ai_priority_history(self):
        # Open the audit log filtered to this picking so the user can see the
        # sequence of recalculations, overrides, and edits.
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id("odoo_picking_priority.action_wms_ai_priority_log")
        action["domain"] = [("picking_id", "=", self.id)]
        action["context"] = {"default_picking_id": self.id}
        return action

    def action_open_ai_priority_popup(self):
        # Pop up the read-only summary wizard for quick inspection.
        self.ensure_one()
        wizard = self.env["wms.ai.priority.popup"].create({"picking_id": self.id})
        return {
            "type": "ir.actions.act_window",
            "name": "Priority Info",
            "res_model": "wms.ai.priority.popup",
            "view_mode": "form",
            "target": "new",
            "res_id": wizard.id,
        }

    def _create_ai_priority_logs(self, action_type="picking_recalculated_manual", action_message="Recalculated picking priority"):
        # Every recalculation, manual override, and edit can leave a structured
        # history record for later debugging.
        log_model = self.env["wms.ai.priority.log"]
        for picking in self:
            final_sla = picking._get_ai_scoring_deadline()
            sla_source = picking._get_ai_sla_source_label()
            log_model.create(
                {
                    "picking_id": picking.id,
                    "action_type": action_type,
                    "action_message": action_message,
                    "score": picking.x_ai_priority_score,
                    "rank": picking.x_ai_priority_rank,
                    "final_human_rank": picking.x_display_priority_rank,
                    "bucket": picking.x_ai_priority_bucket,
                    "factor_sla": picking.x_ai_factor_sla,
                    "factor_availability": picking.x_ai_factor_availability,
                    "factor_urgency": picking.x_ai_factor_urgency,
                    "factor_channel": picking.x_ai_factor_channel,
                    "factor_dependency": picking.x_ai_factor_dependency,
                    "factor_value": picking.x_ai_factor_value,
                    "factor_complexity": picking.x_ai_factor_complexity,
                    "delay_risk": picking.x_ai_delay_risk,
                    "delay_risk_reason": picking.x_ai_delay_risk_reason,
                    "reason_json": picking.x_ai_priority_reason_json,
                    "recommendation_version": picking.x_ai_recommendation_version,
                    "sla_deadline": final_sla,
                    "sla_source": sla_source,
                    "overridden": picking.x_ai_manual_override,
                    "override_user_id": picking.x_ai_override_user_id.id,
                    "override_reason": picking.x_ai_override_reason,
                }
            )

    def _get_ai_priority_factors(self, company_max_order_value):
        # Gather each factor score independently so the final explanation can
        # show where the total came from.
        self.ensure_one()
        config = self._get_ai_factor_config()
        max_order_value = company_max_order_value.get(self.company_id.id, 0.0)
        order_value = self._get_ai_order_value()
        total_demand, total_reserved, availability_ratio = self._get_ai_availability_metrics()
        factor_details = {
            "sla": self._get_ai_sla_factor(config["sla"]),
            "availability": self._get_ai_availability_factor(config["availability"], availability_ratio),
            "urgency": self._get_ai_urgency_factor(config["urgency"]),
            "channel": self._get_ai_channel_factor(config["channel"]),
            "dependency": self._get_ai_dependency_factor(config["dependency"]),
            "value": self._get_ai_value_factor(config["value"], order_value, max_order_value),
            "complexity": self._get_ai_complexity_factor(config["complexity"]),
        }

        factors = {name: values["score"] for name, values in factor_details.items()}
        reason_lines = [
            {
                "factor": factor_name,
                "score": details["score"],
                "label": details["label"],
            }
            for factor_name, details in sorted(
                factor_details.items(),
                key=lambda item: item[1]["score"],
                reverse=True,
            )
            if details["score"] > 0
        ]
        missing_qty = max((total_demand or 0.0) - (total_reserved or 0.0), 0.0)
        if missing_qty > 0:
            reason_lines.append(
                {
                    "factor": "stock_gap",
                    "score": 0.0,
                    "label": "Stock shortage detected: %.2f available, %.2f missing"
                    % (total_reserved or 0.0, missing_qty),
                }
            )
        return factors, reason_lines

    def _recalculate_ai_priority(self, action_type, action_message):
        # Central recalculation helper used by both the manual button and the
        # scheduled cron job.
        self._compute_ai_priority_fields()
        self._compute_ai_priority_tiebreaker()
        self._compute_ai_priority_rank()
        self._compute_display_priority_rank()
        self._compute_manual_priority_rank_display()
        self._create_ai_priority_logs(action_type=action_type, action_message=action_message)
        return True

    @api.model
    def _get_company_max_order_values(self):
        # The value factor is scaled relative to the largest open order per
        # company so it stays comparable inside each active pool.
        values = {}
        for company in self.env.companies:
            open_states = self._get_ai_open_states(company)
            pickings = self.search(
                [
                    ("company_id", "=", company.id),
                    ("state", "in", open_states),
                ]
            )
            order_values = [picking._get_ai_order_value() for picking in pickings]
            values[company.id] = max(order_values or [0.0])
        return values

    @api.model
    def _get_ai_open_states(self, company):
        # Waiting pickings are optional because some installations want them to
        # stay out of the active queue until they are ready.
        if company.x_ai_include_waiting_pickings:
            return ["confirmed", "assigned", "waiting"]
        return list(DEFAULT_OPEN_PICKING_STATES)

    def _get_ai_factor_config(self):
        # Resolve the active factor config in this order:
        # default template -> company/warehouse overrides -> live enabled flags.
        self.ensure_one()
        config_map = {
            factor: {"max": values["max"], "enabled": values["enabled"]}
            for factor, values in DEFAULT_FACTOR_CONFIG.items()
        }
        warehouse = self.picking_type_id.warehouse_id
        domain = [
            ("company_id", "=", self.company_id.id),
            ("active", "=", True),
            "|",
            ("warehouse_id", "=", warehouse.id if warehouse else False),
            ("warehouse_id", "=", False),
        ]
        configs = self.env["wms.ai.priority.config"].search(domain, order="warehouse_id desc, sequence asc, id asc")
        for record in configs:
            config_map[record.factor_name] = {
                "max": max(record.weight_max_score, 0.0),
                "enabled": record.enabled,
            }
        return config_map

    def _get_ai_scoring_deadline(self):
        # Pick the first meaningful deadline available for score computation.
        self.ensure_one()
        if self.x_ai_sla_deadline:
            return fields.Datetime.to_datetime(self.x_ai_sla_deadline)
        auto_sla = self._get_auto_ai_sla_deadline()
        if auto_sla:
            return fields.Datetime.to_datetime(auto_sla)
        if self.date_deadline:
            return fields.Datetime.to_datetime(self.date_deadline)
        if self.scheduled_date:
            return fields.Datetime.to_datetime(self.scheduled_date)
        return False

    def _get_ai_operational_deadline(self):
        # Operational deadline is the stricter timing signal used for urgency and
        # delay risk when a dispatch cutoff exists.
        self.ensure_one()
        if self.x_ai_dispatch_cutoff:
            return fields.Datetime.to_datetime(self.x_ai_dispatch_cutoff)
        if self.x_ai_sla_deadline:
            return fields.Datetime.to_datetime(self.x_ai_sla_deadline)
        auto_sla = self._get_auto_ai_sla_deadline()
        if auto_sla:
            return fields.Datetime.to_datetime(auto_sla)
        if self.date_deadline:
            return fields.Datetime.to_datetime(self.date_deadline)
        if self.scheduled_date:
            return fields.Datetime.to_datetime(self.scheduled_date)
        return False

    def _get_ai_sla_factor(self, config):
        # SLA is a time-to-deadline score that ramps up as the deadline gets
        # closer or passes.
        if not config["enabled"]:
            return {"score": 0.0, "label": "SLA scoring disabled"}

        deadline = self._get_ai_scoring_deadline()
        if not deadline:
            return {"score": 0.0, "label": "No SLA deadline"}

        now = fields.Datetime.now()
        hours_remaining = (deadline - now).total_seconds() / 3600.0
        source_label = self._get_ai_sla_source_label()
        max_score = max(config["max"], 0.0)
        default_max_score = 30.0
        scale = (max_score / default_max_score) if default_max_score else 0.0
        if hours_remaining <= 0:
            score = default_max_score
            label = "%s already overdue" % source_label
        elif hours_remaining <= 2:
            score = default_max_score
            label = "%s within 2 hours" % source_label
        elif hours_remaining <= 4:
            score = 25.0
            label = "%s within 4 hours" % source_label
        elif hours_remaining <= 8:
            score = 20.0
            label = "%s within 8 hours" % source_label
        elif hours_remaining <= 24:
            score = 12.0
            label = "%s within 24 hours" % source_label
        elif hours_remaining <= 48:
            score = 6.0
            label = "%s within 48 hours" % source_label
        else:
            score = 2.0
            label = "%s beyond 48 hours" % source_label
        return {"score": min(max_score, score * scale), "label": label}

    @api.model
    def _scale_ai_factor_score(self, config, base_score, default_max_score):
        # Helper to rescale a default factor score onto the configured maximum.
        max_score = max(config["max"], 0.0)
        if not default_max_score:
            return min(max_score, base_score)
        return min(max_score, base_score * (max_score / default_max_score))

    def _get_ai_sla_source_label(self):
        # Used only for the explanation text so humans can see whether the SLA
        # came from manual input, customer settings, or product lead time.
        self.ensure_one()
        if self.x_ai_sla_deadline:
            return "manual SLA"
        if self._get_partner_customer_sla_deadline():
            return "customer SLA"
        if self._get_sale_lead_time_sla():
            return "product lead time"
        return "SLA"

    def _get_ai_availability_metrics(self):
        # Availability is measured as reserved quantity vs requested quantity.
        self.ensure_one()
        total_demand = sum(self.move_ids.filtered(lambda m: m.state != "cancel").mapped("product_uom_qty"))
        total_reserved = sum(self.move_ids.filtered(lambda m: m.state != "cancel").mapped("quantity"))
        if not total_demand:
            return total_demand, total_reserved, 0.0
        return total_demand, total_reserved, min(total_reserved / total_demand, 1.0)

    def _get_ai_stock_gap_summary(self):
        # Summarize the missing quantities line by line so the reason text can
        # explain why a picking was treated as stock constrained.
        self.ensure_one()
        gap_lines = []
        for move in self.move_ids.filtered(lambda m: m.state != "cancel"):
            demand_qty = move.product_uom_qty or 0.0
            reserved_qty = move.quantity or 0.0
            missing_qty = max(demand_qty - reserved_qty, 0.0)
            if missing_qty <= 0:
                continue
            gap_lines.append(
                "%s: %.2f available, %.2f missing"
                % (move.product_id.display_name, reserved_qty, missing_qty)
            )
        if not gap_lines:
            return "All required quantities are currently reserved."
        return "\n".join(gap_lines[:5])

    def _get_ai_availability_factor(self, config, availability_ratio):
        # More reserved stock means a higher score because the picking can be
        # completed with less replenishment risk.
        if not config["enabled"]:
            return {"score": 0.0, "label": "Availability scoring disabled"}

        if availability_ratio >= 1.0:
            score = self._scale_ai_factor_score(config, 20.0, 20.0)
        elif availability_ratio >= 0.9:
            score = self._scale_ai_factor_score(config, 15.0, 20.0)
        elif availability_ratio >= 0.7:
            score = self._scale_ai_factor_score(config, 8.0, 20.0)
        elif availability_ratio >= 0.4:
            score = self._scale_ai_factor_score(config, 3.0, 20.0)
        else:
            score = 0.0
        label = "%s%% stock reserved" % round(availability_ratio * 100, 0)
        return {"score": score, "label": label}

    def _get_ai_urgency_factor(self, config):
        # Manual urgency is a simple business override that boosts the score.
        if not config["enabled"]:
            return {"score": 0.0, "label": "Urgency scoring disabled"}

        urgency_map = {"critical": 15.0, "high": 10.0, "normal": 0.0}
        label_map = {
            "critical": "Critical urgency flag",
            "high": "High urgency flag",
            "normal": "Normal urgency",
        }
        score = self._scale_ai_factor_score(config, urgency_map.get(self.x_ai_urgency_level or "normal", 0.0), 15.0)
        return {"score": score, "label": label_map.get(self.x_ai_urgency_level or "normal", "Normal urgency")}

    def _get_ai_channel_factor(self, config):
        # Different source channels carry different operational importance.
        if not config["enabled"]:
            return {"score": 0.0, "label": "Channel scoring disabled"}

        channel = self._get_effective_ai_source_channel()
        channel_map = {
            "marketplace": (10.0, "Marketplace / same-day eCommerce order"),
            "retail_store": (8.0, "VIP retail store / flagship store order"),
            "b2b": (5.0, "Standard B2B customer delivery"),
            "store_replenishment": (4.0, "Store replenishment transfer"),
            "internal_transfer": (2.0, "Internal transfer"),
            "other": (3.0, "Other order source"),
        }
        base_score, label = channel_map.get(channel, (3.0, "Other order source"))
        return {"score": self._scale_ai_factor_score(config, base_score, 10.0), "label": label}

    def _get_effective_ai_source_channel(self):
        # Derive the best channel label from explicit input first, then from the
        # picking type when no explicit source channel is available.
        self.ensure_one()
        derived_channel = self._derive_ai_source_channel()
        if derived_channel:
            return derived_channel
        if self.x_ai_source_channel and self.x_ai_source_channel != "other":
            return self.x_ai_source_channel
        if self.picking_type_code == "internal":
            return "internal_transfer"
        if self.picking_type_code == "outgoing":
            return "b2b"
        return "other"

    def _derive_ai_source_channel(self):
        # Use origin text, partner tags, and sale team hints to classify the
        # order source into a more meaningful business channel.
        self.ensure_one()
        sale_order = self.sale_id
        origin_text = " ".join(filter(None, [self.origin or "", sale_order.origin if sale_order else "", sale_order.client_order_ref if sale_order else ""])).lower()
        partner_tags = {tag.name.lower() for tag in self.partner_id.category_id}
        team_name = sale_order.team_id.name.lower() if sale_order and sale_order.team_id else ""

        marketplace_keywords = ("amazon", "flipkart", "marketplace", "website", "ecommerce", "web")
        retail_keywords = ("vip", "flagship", "retail", "store", "shop")
        replenishment_keywords = ("replenishment", "restock", "store refill", "store transfer")

        if any(keyword in origin_text for keyword in marketplace_keywords) or "marketplace" in partner_tags:
            return "marketplace"
        if any(keyword in partner_tags for keyword in retail_keywords) or any(keyword in team_name for keyword in retail_keywords):
            return "retail_store"
        if self.picking_type_code == "internal" and any(keyword in origin_text for keyword in replenishment_keywords):
            return "store_replenishment"
        if self.picking_type_code == "internal":
            return "internal_transfer"
        if sale_order:
            return "b2b"
        return False

    def _get_ai_dependency_factor(self, config):
        # Dependency score measures how badly a delay would affect another
        # process downstream, not just the picking itself.
        if not config["enabled"]:
            return {"score": 0.0, "label": "Dependency scoring disabled"}

        dependency_type = self._get_ai_dependency_type()
        dependency_map = {
            "customer_dispatch_today": (10.0, "Blocks same-day customer dispatch"),
            "store_opening_stock": (8.0, "Blocks store replenishment before opening"),
            "invoice_blocked": (6.0, "Blocks invoice / billing cycle"),
            "production_blocked": (7.0, "Blocks production continuation"),
            "internal_transfer": (2.0, "Internal transfer with limited downstream blocking"),
            "none": (0.0, "No downstream dependency"),
        }
        base_score, label = dependency_map[dependency_type]
        return {"score": self._scale_ai_factor_score(config, base_score, 10.0), "label": label}

    def _get_ai_dependency_type(self):
        # Classify the downstream impact into a small set of operational types
        # so the reason text stays stable and easy to read.
        self.ensure_one()
        dispatch_cutoff = self._get_ai_operational_deadline()
        origin_text = " ".join(
            filter(
                None,
                [
                    (self.origin or "").lower(),
                    (self.sale_id.origin or "").lower() if self.sale_id else "",
                    (self.sale_id.client_order_ref or "").lower() if self.sale_id else "",
                    (self.location_dest_id.complete_name or "").lower() if self.location_dest_id else "",
                ],
            )
        )
        store_keywords = ("store", "retail", "shop", "outlet", "replenishment", "restock")

        if self.picking_type_code == "outgoing" and dispatch_cutoff:
            cutoff_dt = fields.Datetime.to_datetime(dispatch_cutoff)
            if cutoff_dt and cutoff_dt.date() <= fields.Date.today():
                return "customer_dispatch_today"

        if self.picking_type_code == "internal":
            if (
                (self.location_dest_id and self.location_dest_id.replenish_location)
                or any(keyword in origin_text for keyword in store_keywords)
            ):
                return "store_opening_stock"
            return "internal_transfer"

        if any(
            (
                "raw_material_production_id" in move._fields and move.raw_material_production_id
            ) or (
                "production_id" in move._fields and move.production_id
            )
            for move in self.move_ids
        ):
            return "production_blocked"

        if self.sale_id and getattr(self.sale_id, "invoice_status", False) in ("to invoice", "upselling"):
            return "invoice_blocked"

        return "none"

    def _get_ai_value_factor(self, config, order_value, max_order_value):
        # Value score is relative to the largest open order in the company.
        if not config["enabled"] or not max_order_value or not order_value:
            return {"score": 0.0, "label": "No meaningful order value boost"}
        score = min(config["max"], (order_value / max_order_value) * config["max"])
        return {"score": score, "label": "Order value contributes business impact"}

    def _get_ai_order_value(self):
        # Prefer the sale order total, but fall back to estimated move value if
        # the picking is not linked to a sale.
        self.ensure_one()
        if self.sale_id and self.sale_id.amount_total:
            return self.sale_id.amount_total
        order_value = 0.0
        for move in self.move_ids.filtered(lambda m: m.state != "cancel" and m.product_id):
            quantity = move.product_uom_qty or move.quantity or 0.0
            if move.product_uom and move.product_id and move.product_id.uom_id and move.product_uom != move.product_id.uom_id:
                quantity = move.product_uom._compute_quantity(quantity, move.product_id.uom_id, rounding_method="HALF-UP")
            order_value += quantity * (move.product_id.lst_price or 0.0)
        return order_value

    def _get_ai_complexity_factor(self, config):
        # Smaller product/zone counts are treated as quick wins, while large
        # multi-zone pickings are scored as more complex.
        if not config["enabled"]:
            return {"score": 0.0, "label": "Complexity scoring disabled"}

        product_count, zone_count = self._get_ai_complexity_metrics()
        if product_count <= 3 and zone_count <= 1:
            score = self._scale_ai_factor_score(config, 5.0, 5.0)
            label = "Simple quick-win product picking"
        elif product_count <= 8 and zone_count <= 1:
            score = self._scale_ai_factor_score(config, 3.0, 5.0)
            label = "Medium complexity product picking"
        else:
            score = self._scale_ai_factor_score(config, 1.0, 5.0)
            label = "High complexity multi-product picking"
        return {"score": score, "label": label}

    def _get_ai_complexity_metrics(self):
        # Complexity is based on how many distinct products and source zones are
        # involved in the picking.
        self.ensure_one()
        source_locations = self._get_ai_complexity_source_locations()
        product_count = len({
            move.product_id.id
            for move in self.move_ids.filtered(lambda m: m.state != "cancel")
            if move.product_id
        })
        zone_names = {self._get_ai_zone_name(location) for location in source_locations}
        zone_count = len({name for name in zone_names if name})
        return product_count, zone_count

    def _get_ai_complexity_source_locations(self):
        # Prefer move line locations when available because they show the real
        # picking path more accurately than the move header alone.
        self.ensure_one()
        source_locations = self.env["stock.location"]
        active_moves = self.move_ids.filtered(lambda m: m.state != "cancel")
        for move in active_moves:
            move_locations = move.move_line_ids.filtered(lambda ml: ml.state != "cancel")
            if move_locations:
                for move_line in move_locations:
                    location = move_line.quant_id.location_id or move_line.location_id or move.location_id
                    if location:
                        source_locations |= location
                continue
            if move.location_id:
                source_locations |= move.location_id
        return source_locations

    def _get_ai_zone_name(self, location):
        # Extract a readable zone label from the full location path.
        self.ensure_one()
        location_name = (location.display_name or location.name or "").strip()
        if not location_name:
            return ""
        path_parts = [part.strip() for part in location_name.split("/") if part.strip()]
        for part in path_parts:
            if "zone" in part.lower():
                return part
        if len(path_parts) >= 3:
            return path_parts[2]
        if len(path_parts) >= 2:
            return path_parts[-2]
        return path_parts[0]

    @api.depends("move_ids.state", "move_ids.product_id", "move_ids.location_id")
    def _compute_ai_complexity_debug(self):
        # The debug fields are intentionally verbose so warehouse admins can see
        # why a picking was classified as simple or complex.
        for picking in self:
            product_count, zone_count = picking._get_ai_complexity_metrics()
            config = picking._get_ai_factor_config()["complexity"]
            factor_details = picking._get_ai_complexity_factor(config)
            debug_lines = []
            for move in picking.move_ids.filtered(lambda m: m.state != "cancel" and m.product_id):
                line_locations = move.move_line_ids.filtered(lambda ml: ml.state != "cancel")
                location_records = picking.env["stock.location"]
                if line_locations:
                    for move_line in line_locations:
                        location_records |= move_line.quant_id.location_id or move_line.location_id or move.location_id
                elif move.location_id:
                    location_records |= move.location_id
                location_names = [((loc.complete_name or loc.display_name or loc.name or "").strip()) for loc in location_records]
                zone_names = [picking._get_ai_zone_name(loc) for loc in location_records]
                product_name = move.product_id.display_name or move.product_id.name or ""
                debug_lines.append(
                    "%s -> %s%s" % (
                        product_name,
                        ", ".join([name for name in zone_names if name]) or "No zone",
                        " (%s)" % ", ".join([name for name in location_names if name]) if location_names else "",
                    )
                )
            picking.x_ai_complexity_product_count = product_count
            picking.x_ai_complexity_zone_count = zone_count
            picking.x_ai_complexity_debug_score = factor_details["score"]
            picking.x_ai_complexity_debug_summary = (
                "Products: %s\nZones: %s\nRule: %s\nScore: %s"
                % (
                    product_count,
                    zone_count,
                    factor_details["label"],
                    factor_details["score"],
                )
            )
            picking.x_ai_complexity_debug_details = "\n".join(debug_lines) if debug_lines else "No active product moves found."

    @api.model
    def _get_ai_priority_bucket(self, score):
        # Bucket thresholds convert the numeric score into a warehouse-friendly
        # urgency label.
        if score >= 85:
            return "critical"
        if score >= 70:
            return "high"
        if score >= 50:
            return "medium"
        return "low"

    def _get_ai_delay_risk(self, total_score, factors):
        # Delay risk combines deadline pressure, dependency type, and readiness
        # into a separate operational warning signal.
        self.ensure_one()
        dependency_type = self._get_ai_dependency_type()
        deadline = self._get_ai_operational_deadline()
        _, _, availability_ratio = self._get_ai_availability_metrics()

        if deadline:
            hours_remaining = (deadline - fields.Datetime.now()).total_seconds() / 3600.0
        else:
            hours_remaining = None

        time_reference = self._get_ai_delay_time_reference(hours_remaining)

        if hours_remaining is not None and hours_remaining <= 2:
            if hours_remaining <= 0:
                return "critical", "%s Immediate action is required." % time_reference
            return "critical", "%s Immediate action is required." % time_reference
        if dependency_type == "customer_dispatch_today" and availability_ratio < 1.0:
            return "critical", "%s Delay could block same-day customer dispatch while stock is not fully ready." % (
                time_reference or "The order is time-sensitive."
            )
        if dependency_type in ("customer_dispatch_today", "store_opening_stock") or total_score >= 85:
            if time_reference:
                return "high", "%s Delay may block a time-sensitive downstream operation." % time_reference
            return "high", "Delay may block a time-sensitive downstream operation."
        if factors["sla"] >= 12.0 or factors["dependency"] >= 6.0 or availability_ratio < 0.7:
            if time_reference:
                return "medium", "%s Delay may affect service level or require extra warehouse follow-up." % time_reference
            return "medium", "Delay may affect service level or require extra warehouse follow-up."
        if time_reference:
            return "low", "%s Delay risk is currently limited compared with other open pickings." % time_reference
        return "low", "No immediate deadline pressure is currently detected."

    @api.model
    def _get_ai_delay_time_reference(self, hours_remaining):
        # Pre-format the deadline distance so the risk explanation can read like
        # an operations note instead of a raw number.
        if hours_remaining is None:
            return False
        if hours_remaining < 0:
            return "The SLA deadline was missed %s ago." % self._format_ai_time_gap(abs(hours_remaining))
        return "%s remain before the SLA deadline." % self._format_ai_time_gap(hours_remaining)

    @api.model
    def _format_ai_time_gap(self, hours_value):
        # Turn a decimal hour value into a friendly days / hours / minutes
        # string.
        total_minutes = max(int(round((hours_value or 0.0) * 60)), 0)
        days, remainder_minutes = divmod(total_minutes, 1440)
        hours, minutes = divmod(remainder_minutes, 60)
        parts = []
        if days:
            parts.append("%s day%s" % (days, "" if days == 1 else "s"))
        if hours:
            parts.append("%s hour%s" % (hours, "" if hours == 1 else "s"))
        if minutes:
            parts.append("%s minute%s" % (minutes, "" if minutes == 1 else "s"))
        if not parts:
            return "less than 1 minute"
        if len(parts) == 1:
            return parts[0]
        return "%s and %s" % (", ".join(parts[:-1]), parts[-1])

    @api.model
    def _get_ai_recommended_action(self, delay_risk, factors, total_demand=None, total_reserved=None):
        # Recommendation is intentionally simple: prioritize stock gaps first,
        # then critical risk, then urgent review cases.
        if total_demand is None or total_reserved is None:
            total_demand, total_reserved, _ = self._get_ai_availability_metrics()
        missing_qty = max((total_demand or 0.0) - (total_reserved or 0.0), 0.0)
        if missing_qty > 0 and total_reserved > 0:
            return "pick_available_and_replenish"
        if missing_qty > 0 and total_reserved <= 0:
            return "expedite_stock"
        if delay_risk == "critical":
            return "pick_now"
        if delay_risk == "high":
            return "pick_next"
        if factors["urgency"] >= 10 and factors["dependency"] >= 6:
            return "review_override"
        return "monitor"

    @api.model
    def _build_ai_reason_text(self, score, reason_lines, delay_risk_reason=None):
        # Build the human-facing explanation shown in the popup, chatter, and
        # audit log.
        if not reason_lines:
            lines = ["Priority score %.2f/100. No positive factors applied." % score]
            if delay_risk_reason:
                lines.extend(["", "If delayed:", delay_risk_reason])
            return "\n".join(lines)
        lines = ["Priority = %.2f/100" % score, "", "Main reasons:"]
        for index, reason in enumerate(reason_lines[:5], start=1):
            if reason["score"] > 0:
                lines.append("%s. %s (+%.2f)" % (index, reason["label"], reason["score"]))
            else:
                lines.append("%s. %s" % (index, reason["label"]))
        if delay_risk_reason:
            lines.extend(["", "If delayed:", delay_risk_reason])
        return "\n".join(lines)


class ResCompany(models.Model):
    # Company-level toggles that control which pickings enter the AI queue and
    # whether an external scoring engine should be used.
    _inherit = "res.company"

    x_ai_include_waiting_pickings = fields.Boolean(
        string="Include Waiting Pickings in Priority Scoring",
        default=False,
    )
    x_ai_use_external_scoring = fields.Boolean(
        string="Use External Scoring Service",
        default=False,
    )
    x_ai_scoring_endpoint = fields.Char(string="Scoring Endpoint")
    x_ai_scoring_token = fields.Char(string="Scoring Token")
    x_ai_scoring_timeout = fields.Integer(string="Scoring Timeout (Seconds)", default=5)
