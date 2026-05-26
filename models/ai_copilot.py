import json
import logging
import re
from datetime import datetime, time, timedelta

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .http_json import JsonHttpRequestError, post_json
from .stock_picking_priority import DEFAULT_FACTOR_CONFIG, FACTOR_LABELS, MAX_PRIORITY_SCORE

_logger = logging.getLogger(__name__)

# These parameters define the runtime AI setup used across all picking-related
# wizards. The module supports OpenRouter, OpenAI, and Gemini with the same
# high-level user flows.
AI_PROVIDER_PARAM = "odoo_picking_priority.ai_provider"
AI_BASE_URL_PARAM = "odoo_picking_priority.ai_base_url"
AI_API_KEY_PARAM = "odoo_picking_priority.ai_api_key"
AI_MODEL_PARAM = "odoo_picking_priority.ai_model"
AI_SITE_URL_PARAM = "odoo_picking_priority.ai_site_url"
AI_APP_NAME_PARAM = "odoo_picking_priority.ai_app_name"
OPENROUTER_BASE_URL_PARAM = "odoo_picking_priority.openrouter_base_url"
OPENROUTER_API_KEY_PARAM = "odoo_picking_priority.openrouter_api_key"
OPENROUTER_MODEL_PARAM = "odoo_picking_priority.openrouter_model"
OPENROUTER_SITE_URL_PARAM = "odoo_picking_priority.openrouter_site_url"
OPENROUTER_APP_NAME_PARAM = "odoo_picking_priority.openrouter_app_name"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


class WmsAiCopilotMixin(models.AbstractModel):
    # Shared AI plumbing for all picking assistant wizards. Keeping the API
    # calls, JSON parsing, and fallback behavior here prevents each wizard from
    # having to repeat the same provider logic.
    _name = "wms.ai.copilot.mixin"
    _description = "WMS AI Copilot Mixin"

    @api.model
    def _log_audit_event(self, action_type, action_message, **kwargs):
        # Logging is best-effort. A failed audit write should never break the
        # user-facing AI flow.
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
        # Store structured data in a consistent text form so it can be copied
        # into audit rows and chatter entries.
        if value in (None, False):
            return False
        if isinstance(value, str):
            return value
        return json.dumps(value, indent=2, default=str)

    @api.model
    def _get_ai_settings(self):
        # Read the saved system parameters and normalize them into a single
        # provider settings dict for the rest of the module.
        params = self.env["ir.config_parameter"].sudo()
        provider = (params.get_param(AI_PROVIDER_PARAM) or "openrouter").strip().lower()
        api_key = (params.get_param(AI_API_KEY_PARAM) or params.get_param(OPENROUTER_API_KEY_PARAM) or "").strip()
        model = (params.get_param(AI_MODEL_PARAM) or params.get_param(OPENROUTER_MODEL_PARAM) or "").strip()
        site_url = (params.get_param(AI_SITE_URL_PARAM) or params.get_param(OPENROUTER_SITE_URL_PARAM) or "").strip()
        app_name = (
            params.get_param(AI_APP_NAME_PARAM) or params.get_param(OPENROUTER_APP_NAME_PARAM) or "Odoo Picking Priority Agent"
        ).strip()
        configured_base_url = (params.get_param(AI_BASE_URL_PARAM) or "").strip()
        default_base_url = {
            "openrouter": DEFAULT_OPENROUTER_BASE_URL,
            "openai": DEFAULT_OPENAI_BASE_URL,
            "gemini": DEFAULT_GEMINI_BASE_URL,
        }.get(provider, DEFAULT_OPENROUTER_BASE_URL)
        return {
            "provider": provider,
            "base_url": (configured_base_url or default_base_url).rstrip("/"),
            "api_key": api_key,
            "model": model,
            "site_url": site_url,
            "app_name": app_name,
        }

    @api.model
    def _get_openrouter_settings(self):
        return self._get_ai_settings()

    @api.model
    def _is_ai_configured(self):
        # A provider is considered usable only when both model and API key are
        # present.
        settings = self._get_ai_settings()
        return bool(settings["api_key"] and settings["model"])

    @api.model
    def _is_openrouter_configured(self):
        return self._is_ai_configured()

    @api.model
    def _call_ai_provider(self, system_prompt, user_prompt, temperature=0.2):
        # Convenience wrapper for callers that just want to use the saved
        # configuration.
        settings = self._get_ai_settings()
        return self._call_ai_provider_with_settings(settings, system_prompt, user_prompt, temperature)

    @api.model
    def _call_ai_provider_with_settings(self, settings, system_prompt, user_prompt, temperature=0.2):
        # All providers are driven through the same two-message chat pattern so
        # the prompts stay easy to reason about.
        if not settings.get("api_key") or not settings.get("model"):
            raise UserError(
                _("AI provider is not configured. Set AI Provider, AI Model, and API Key first.")
            )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        provider = (settings.get("provider") or "").strip().lower()
        if provider == "openrouter":
            return self._call_openrouter_provider(settings, messages, temperature)
        if provider == "openai":
            return self._call_openai_provider(settings, messages, temperature)
        if provider == "gemini":
            return self._call_gemini_provider(settings, messages, temperature)
        raise UserError(_("Unsupported AI provider '%s'. Use openrouter, openai, or gemini.") % provider)

    @api.model
    def _call_openrouter(self, system_prompt, user_prompt, temperature=0.2):
        return self._call_ai_provider(system_prompt, user_prompt, temperature=temperature)

    @api.model
    def _call_openrouter_provider(self, settings, messages, temperature):
        # OpenRouter uses the OpenAI-style chat/completions endpoint with a few
        # optional metadata headers for referer and app name.
        headers = {
            "Authorization": "Bearer %s" % settings["api_key"],
            "Content-Type": "application/json",
        }
        if settings["site_url"]:
            headers["HTTP-Referer"] = settings["site_url"]
        if settings["app_name"]:
            headers["X-Title"] = settings["app_name"]
        payload = {
            "model": settings["model"],
            "temperature": temperature,
            "messages": messages,
        }
        try:
            data = post_json("%s/chat/completions" % settings["base_url"], payload, headers=headers, timeout=45)
        except JsonHttpRequestError as error:
            raise UserError(_("OpenRouter request failed: %s") % error) from error
        return self._extract_chat_response(data, provider_name="OpenRouter")

    @api.model
    def _call_openai_provider(self, settings, messages, temperature):
        # OpenAI follows the same request shape as OpenRouter, so this stays
        # intentionally parallel to the method above.
        headers = {
            "Authorization": "Bearer %s" % settings["api_key"],
            "Content-Type": "application/json",
        }
        payload = {
            "model": settings["model"],
            "temperature": temperature,
            "messages": messages,
        }
        try:
            data = post_json("%s/chat/completions" % settings["base_url"], payload, headers=headers, timeout=45)
        except JsonHttpRequestError as error:
            raise UserError(_("OpenAI request failed: %s") % error) from error
        return self._extract_chat_response(data, provider_name="OpenAI")

    @api.model
    def _call_gemini_provider(self, settings, messages, temperature):
        # Gemini expects a flattened prompt payload instead of a chat message
        # list, so we concatenate the message contents before sending.
        prompt_text = "\n\n".join(message["content"] for message in messages if message.get("content"))
        payload = {
            "contents": [{"parts": [{"text": prompt_text}]}],
            "generationConfig": {
                "temperature": temperature,
            },
        }
        url = "%s/%s:generateContent?key=%s" % (settings["base_url"], settings["model"], settings["api_key"])
        try:
            data = post_json(url, payload, headers={"Content-Type": "application/json"}, timeout=45)
        except JsonHttpRequestError as error:
            raise UserError(_("Gemini request failed: %s") % error) from error
        candidates = data.get("candidates") or []
        if not candidates:
            raise UserError(_("Gemini returned no candidates."))
        parts = candidates[0].get("content", {}).get("parts") or []
        if not parts:
            raise UserError(_("Gemini returned no content parts."))
        return "".join(part.get("text", "") for part in parts).strip()

    @api.model
    def _extract_chat_response(self, data, provider_name="AI provider"):
        # Small helper for OpenAI-compatible responses.
        choices = data.get("choices") or []
        if not choices:
            raise UserError(_("%s returned no choices.") % provider_name)
        return choices[0].get("message", {}).get("content", "").strip()

    @api.model
    def _extract_json_object(self, content):
        # The model may wrap JSON in prose or fenced code blocks, so we trim
        # that noise and extract the first object-shaped payload.
        candidate = (content or "").strip()
        if not candidate:
            raise ValueError("Empty AI response.")
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", candidate, re.S)
        if fenced:
            candidate = fenced.group(1).strip()
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise ValueError("No JSON object found in AI response.")
        return json.loads(candidate[start : end + 1])

    @api.model
    def _normalize_policy_weights(self, weights):
        # Normalize every supported factor onto the module's fixed 100-point
        # budget so policies remain comparable and deterministic.
        normalized = {}
        for factor_name in DEFAULT_FACTOR_CONFIG:
            normalized[factor_name] = max(float(weights.get(factor_name, 0.0)), 0.0)
        total = sum(normalized.values())
        if total <= 0:
            raise ValueError("AI response did not include any usable supported factor weights.")
        normalized = {
            factor_name: round((weight / total) * MAX_PRIORITY_SCORE, 2)
            for factor_name, weight in normalized.items()
        }
        rounding_gap = round(MAX_PRIORITY_SCORE - sum(normalized.values()), 2)
        normalized["sla"] = round(normalized["sla"] + rounding_gap, 2)
        return normalized

    @api.model
    def _looks_like_low_signal_policy(self, normalized_weights, prompt_text):
        # Reject near-uniform outputs when the prompt clearly contains strong
        # business words, because that usually means the model missed the point.
        values = list(normalized_weights.values())
        if not values:
            return True
        spread = max(values) - min(values)
        prompt_text = (prompt_text or "").lower()
        business_keywords = (
            "internal transfer",
            "internal transfers",
            "marketplace",
            "b2b",
            "wholesale",
            "sla",
            "deadline",
            "availability",
            "replenishment",
            "invoice",
            "urgent",
            "urgency",
        )
        has_business_signal = any(keyword in prompt_text for keyword in business_keywords)
        return has_business_signal and spread <= 2.0

    @api.model
    def _validate_policy_proposal_data(self, proposal):
        # Validation keeps the scoring engine safe: all supported factors must be
        # present, non-negative, and total exactly 100.
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

    @api.model
    def _build_policy_preview_text_data(self, proposal):
        # Turn the JSON proposal into a compact manager-friendly summary.
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

    @api.model
    def _safe_json(self, value):
        return json.dumps(value, indent=2)

    @api.model
    def _heuristic_policy_proposal(self, prompt_text, policy_name):
        # Fallback scoring policy builder used when no AI provider is available.
        normalized_prompt = (prompt_text or "").strip().lower()
        weights = {factor_name: values["max"] for factor_name, values in DEFAULT_FACTOR_CONFIG.items()}
        notes = []
        special_rules = []

        def apply_adjustment(factor_name, delta, note):
            weights[factor_name] = max(weights[factor_name] + delta, 0.0)
            notes.append(note)

        if any(keyword in normalized_prompt for keyword in ("sla", "deadline", "dispatch", "cutoff", "cut-off")):
            apply_adjustment("sla", 10.0, "Prompt emphasizes dispatch deadline / SLA urgency.")
        if any(
            keyword in normalized_prompt
            for keyword in ("stock readiness", "stock ready", "availability", "reserved", "full availability", "readiness")
        ):
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
            apply_adjustment("complexity", 3.0, "Prompt favors quick-win or simple product pickings.")

        return {
            "policy_name": policy_name,
            "source_prompt": prompt_text,
            "weights_by_context": {"default": self._normalize_policy_weights(weights)},
            "special_rules": special_rules,
            "notes": notes or ["Default rule-based scoring template retained with no strong adjustments detected."],
        }

    @api.model
    def _build_policy_proposal_with_ai(self, prompt_text, policy_name):
        # Ask the provider to produce strict JSON, then validate and normalize
        # the response before any downstream wizard sees it.
        system_prompt = """
You are a warehouse operations configuration assistant.
Convert the user's picking priority policy request into strict JSON.
Supported factors only:
- sla
- availability
- urgency
- channel
- dependency
- value
- complexity
Return JSON with:
{
  "policy_name": "...",
  "weights_by_context": {
    "default": {
      "sla": number,
      "availability": number,
      "urgency": number,
      "channel": number,
      "dependency": number,
      "value": number,
      "complexity": number
    }
  },
  "special_rules": ["..."],
  "notes": ["..."]
}
Do not include unsupported factors or executable stock operations.
""".strip()
        user_prompt = "Policy name: %s\nPrompt:\n%s" % (policy_name, prompt_text)
        content = self._call_ai_provider(system_prompt, user_prompt, temperature=0.1)
        proposal = self._extract_json_object(content)
        proposal["policy_name"] = proposal.get("policy_name") or policy_name
        proposal["source_prompt"] = prompt_text
        weights_by_context = proposal.get("weights_by_context") or {}
        default_weights = weights_by_context.get("default") or {}
        supported_weight_count = len([factor_name for factor_name in DEFAULT_FACTOR_CONFIG if factor_name in default_weights])
        if supported_weight_count == 0:
            raise ValueError("AI response did not include supported factor keys in weights_by_context.default.")
        normalized_weights = self._normalize_policy_weights(default_weights)
        if self._looks_like_low_signal_policy(normalized_weights, prompt_text):
            raise ValueError("AI response produced a low-signal near-uniform policy.")
        proposal["weights_by_context"] = {"default": normalized_weights}
        proposal["special_rules"] = [str(rule) for rule in (proposal.get("special_rules") or []) if str(rule).strip()][:10]
        proposal["notes"] = [str(note) for note in (proposal.get("notes") or []) if str(note).strip()][:10]
        return proposal

    @api.model
    def _build_policy_proposal(self, prompt_text, policy_name):
        # Prefer the AI-generated version when configured; otherwise fall back to
        # the keyword-based heuristic version.
        if self._is_ai_configured():
            try:
                return self._build_policy_proposal_with_ai(prompt_text, policy_name)
            except Exception as error:
                _logger.exception("AI policy proposal failed for prompt '%s'", policy_name)
                raise UserError(_("AI policy proposal failed: %s") % error) from error
        return self._heuristic_policy_proposal(prompt_text, policy_name)


class WmsAiPriorityPolicyPrompt(models.Model):
    # This inherit-only model wires the shared AI mixin into the existing policy
    # prompt wizard.
    _inherit = ["wms.ai.priority.policy.prompt", "wms.ai.copilot.mixin"]

    def _build_policy_proposal_from_prompt(self, prompt_text):
        self.ensure_one()
        return self._build_policy_proposal(prompt_text, self.name or "Prompt Draft")

    def _validate_policy_proposal(self, proposal):
        return self._validate_policy_proposal_data(proposal)

    def _build_policy_preview_text(self, proposal):
        return self._build_policy_preview_text_data(proposal)


class WmsAiPickingAssistant(models.TransientModel):
    # Single-picking chat assistant used to explain why an order is urgent or
    # blocked without changing any data.
    _name = "wms.ai.picking.assistant"
    _description = "WMS Picking AI Assistant"
    _inherit = "wms.ai.copilot.mixin"

    picking_id = fields.Many2one("stock.picking", required=True, readonly=True)
    question = fields.Text(required=True, string="Ask AI")
    answer_text = fields.Text(readonly=True, string="AI Response")
    prompt_context = fields.Text(readonly=True, string="Context Snapshot")
    response_status = fields.Selection(
        [("draft", "Draft"), ("answered", "Answered"), ("fallback", "Fallback Answer")],
        default="draft",
        readonly=True,
    )

    def action_ask_ai(self):
        # Use the live picking snapshot as context and fall back to a local
        # explanation when the provider is unavailable.
        for wizard in self:
            context_payload = wizard.picking_id._get_ai_assistant_context_payload()
            wizard.prompt_context = self._safe_json(context_payload)
            fallback_answer = wizard.picking_id._build_ai_assistant_fallback_answer(wizard.question)
            answer = fallback_answer
            status = "fallback"
            if self._is_ai_configured():
                try:
                    system_prompt = """
You are an enterprise warehouse copilot for Odoo.
Answer using the provided picking context only.
Explain why the picking is urgent, blocked, or risky.
If the user asks what to do, provide an advisory next action only.
Do not claim you changed data or executed warehouse operations.
Keep the answer concise and operational.
""".strip()
                    user_prompt = "Picking context:\n%s\n\nQuestion:\n%s" % (
                        self._safe_json(context_payload),
                        wizard.question,
                    )
                    answer = self._call_ai_provider(system_prompt, user_prompt, temperature=0.2)
                    status = "answered"
                except Exception as error:
                    _logger.exception("Ask AI failed for picking %s", wizard.picking_id.name)
                    wizard._log_audit_event(
                        "ai_question",
                        "AI question failed for picking %s" % wizard.picking_id.display_name,
                        company_id=wizard.picking_id.company_id.id,
                        picking_id=wizard.picking_id.id,
                        reason_json=wizard._audit_json(
                            {
                                "question": wizard.question,
                                "context_payload": context_payload,
                                "response_status": "failed",
                                "error": str(error),
                            }
                        ),
                    )
                    raise UserError(_("Ask AI failed: %s") % error) from error
            wizard.write({"answer_text": answer, "response_status": status})
            wizard._log_audit_event(
                "ai_question",
                "AI question %s for picking %s" % (status, wizard.picking_id.display_name),
                company_id=wizard.picking_id.company_id.id,
                picking_id=wizard.picking_id.id,
                reason_json=wizard._audit_json(
                    {
                        "question": wizard.question,
                        "answer_text": answer,
                        "response_status": status,
                        "context_payload": context_payload,
                    }
                ),
            )
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Ask AI",
            "res_model": "wms.ai.picking.assistant",
            "view_mode": "form",
            "target": "new",
            "res_id": self.id,
        }


class WmsAiPriorityQueueSummary(models.TransientModel):
    # Supervisor-facing queue summary wizard with AI and deterministic fallback
    # summaries.
    _name = "wms.ai.priority.queue.summary"
    _description = "WMS AI Queue Summary"
    _inherit = "wms.ai.copilot.mixin"

    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)
    warehouse_id = fields.Many2one("stock.warehouse")
    include_waiting = fields.Boolean(default=False)
    summary_text = fields.Text(readonly=True)
    snapshot_json = fields.Text(readonly=True)
    response_status = fields.Selection(
        [("draft", "Draft"), ("generated", "Generated"), ("fallback", "Fallback Summary")],
        default="draft",
        readonly=True,
    )

    def action_generate_summary(self):
        # Build the snapshot, ask the model for a summary, and store both the
        # payload and the text output for auditing.
        for wizard in self:
            pickings = wizard._get_queue_pickings()
            payload = wizard._build_queue_payload(pickings)
            fallback_summary = wizard._build_fallback_queue_summary(payload)
            summary = fallback_summary
            status = "fallback"
            if self._is_ai_configured():
                try:
                    system_prompt = """
You are a warehouse operations copilot.
Summarize queue risk for a supervisor.
Mention SLA pressure, stock readiness, blocked items, and the best first wave.
Do not invent actions that were not requested.
""".strip()
                    user_prompt = "Queue snapshot:\n%s" % self._safe_json(payload)
                    summary = self._call_ai_provider(system_prompt, user_prompt, temperature=0.2)
                    status = "generated"
                except Exception as error:
                    _logger.exception("Queue summary AI failed")
                    wizard._log_audit_event(
                        "ai_queue_summary",
                        "Queue summary generation failed",
                        company_id=wizard.company_id.id,
                        reason_json=wizard._audit_json(
                            {
                                "company_id": wizard.company_id.id,
                                "warehouse_id": wizard.warehouse_id.id if wizard.warehouse_id else False,
                                "include_waiting": wizard.include_waiting,
                                "payload": payload,
                                "response_status": "failed",
                                "error": str(error),
                            }
                        ),
                    )
                    raise UserError(_("Queue summary AI failed: %s") % error) from error
            wizard.write({"snapshot_json": self._safe_json(payload), "summary_text": summary, "response_status": status})
            wizard._log_audit_event(
                "ai_queue_summary",
                "Queue summary %s for %s" % (status, wizard.warehouse_id.display_name if wizard.warehouse_id else "all warehouses"),
                company_id=wizard.company_id.id,
                reason_json=wizard._audit_json(
                    {
                        "company_id": wizard.company_id.id,
                        "warehouse_id": wizard.warehouse_id.id if wizard.warehouse_id else False,
                        "include_waiting": wizard.include_waiting,
                        "payload": payload,
                        "summary_text": summary,
                        "response_status": status,
                    }
                ),
            )
        return True

    def _get_queue_pickings(self):
        # Query the active picking queue in score order, with optional waiting
        # records if company policy allows them.
        self.ensure_one()
        stock_picking = self.env["stock.picking"]
        open_states = ["confirmed", "assigned"]
        if self.include_waiting or self.company_id.x_ai_include_waiting_pickings:
            open_states.append("waiting")
        domain = [("company_id", "=", self.company_id.id), ("state", "in", open_states)]
        if self.warehouse_id:
            domain.append(("picking_type_id.warehouse_id", "=", self.warehouse_id.id))
        return stock_picking.search(
            domain,
            order="x_ai_priority_score desc, x_ai_sla_deadline asc, create_date asc, id asc",
            limit=30,
        )

    def _build_queue_payload(self, pickings):
        # Keep the payload small and operationally useful: counts, top pickings,
        # and current risk labels.
        top_wave = []
        bucket_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        risk_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        blocked = 0
        for picking in pickings:
            bucket_counts[picking.x_ai_priority_bucket or "low"] += 1
            risk_counts[picking.x_ai_delay_risk or "low"] += 1
            if picking.x_ai_recommended_action == "expedite_stock":
                blocked += 1
            if len(top_wave) < 5:
                top_wave.append(
                    {
                        "name": picking.name,
                        "score": picking.x_ai_priority_score,
                        "delay_risk": picking.x_ai_delay_risk,
                        "recommended_action": picking.x_ai_recommended_action,
                    }
                )
        return {
            "company": self.company_id.display_name,
            "warehouse": self.warehouse_id.display_name if self.warehouse_id else "All Warehouses",
            "open_pickings": len(pickings),
            "priority_buckets": bucket_counts,
            "delay_risks": risk_counts,
            "blocked_due_to_stock": blocked,
            "top_wave_candidates": top_wave,
        }

    def _build_fallback_queue_summary(self, payload):
        # Deterministic summary used when no AI provider is configured.
        lines = [
            "Queue summary for %s" % payload["warehouse"],
            "%s open pickings are in the active queue." % payload["open_pickings"],
            "%s critical and %s high-priority pickings are currently open."
            % (payload["priority_buckets"]["critical"], payload["priority_buckets"]["high"]),
            "%s pickings are blocked by stock readiness and likely need replenishment follow-up."
            % payload["blocked_due_to_stock"],
        ]
        if payload["top_wave_candidates"]:
            lines.append(
                "Suggested first wave: %s."
                % ", ".join(candidate["name"] for candidate in payload["top_wave_candidates"])
            )
        return "\n".join(lines)


class WmsAiPriorityAiConfigWizard(models.TransientModel):
    # Temporary configuration wizard used to validate credentials before saving
    # them into system parameters.
    _name = "wms.ai.priority.ai.config.wizard"
    _description = "WMS Picking Priority AI Configuration"
    _inherit = "wms.ai.copilot.mixin"

    provider = fields.Selection(
        [("openrouter", "OpenRouter"), ("openai", "OpenAI"), ("gemini", "Gemini")],
        string="AI Provider",
        required=True,
        default="openrouter",
    )
    model = fields.Char(string="AI Model", required=True)
    api_key = fields.Char(string="API Key", required=True)
    test_result = fields.Char(string="Test Result", readonly=True)

    @api.model
    def default_get(self, fields_list):
        # Pre-fill the wizard with whatever is already configured so the user can
        # edit the existing setup instead of starting from scratch.
        res = super().default_get(fields_list)
        settings = self._get_ai_settings()
        res.update(
            {
                "provider": settings.get("provider") or "openrouter",
                "model": settings.get("model") or "",
                "api_key": settings.get("api_key") or "",
            }
        )
        return res

    def _build_runtime_settings(self):
        # Build a transient provider settings dict without touching persisted
        # configuration yet.
        self.ensure_one()
        provider = (self.provider or "openrouter").strip().lower()
        base_url = {
            "openrouter": DEFAULT_OPENROUTER_BASE_URL,
            "openai": DEFAULT_OPENAI_BASE_URL,
            "gemini": DEFAULT_GEMINI_BASE_URL,
        }.get(provider, DEFAULT_OPENROUTER_BASE_URL)
        return {
            "provider": provider,
            "model": (self.model or "").strip(),
            "api_key": (self.api_key or "").strip(),
            "base_url": base_url,
        }

    def _test_connection(self):
        # Smoke-test the selected provider before allowing the credentials to be
        # saved.
        self.ensure_one()
        settings = self._build_runtime_settings()
        result = self._call_ai_provider_with_settings(
            settings,
            "You are a connection test for the Odoo Picking Priority AI configuration.",
            "Reply with exactly: CONNECTION OK",
            temperature=0.0,
        )
        return _("Connection successful: %s") % (result or "Connection OK")

    def action_save_configuration(self):
        # Save only after the test request succeeds.
        self.ensure_one()
        try:
            self._test_connection()
            params = self.env["ir.config_parameter"].sudo()
            params.set_param(AI_PROVIDER_PARAM, self.provider or "openrouter")
            params.set_param(AI_MODEL_PARAM, self.model or "")
            params.set_param(AI_API_KEY_PARAM, self.api_key or "")
            self._log_audit_event(
                "ai_configuration_saved",
                "AI configuration saved",
                company_id=self.env.company.id,
                reason_json=self._audit_json(
                    {
                        "provider": self.provider,
                        "model": self.model,
                        "api_key_set": bool(self.api_key),
                    }
                ),
            )
        except Exception as error:
            self._log_audit_event(
                "ai_configuration_saved",
                "AI configuration save failed",
                company_id=self.env.company.id,
                reason_json=self._audit_json(
                    {
                        "provider": self.provider,
                        "model": self.model,
                        "api_key_set": bool(self.api_key),
                        "error": str(error),
                    }
                ),
            )
            raise
        return {"type": "ir.actions.act_window_close"}

    def action_test_connection(self):
        # Let the user verify the configuration without committing it to system
        # parameters first.
        self.ensure_one()
        try:
            result = self._test_connection()
            self.write({"test_result": result})
            self._log_audit_event(
                "ai_connection_test",
                "AI connection test succeeded",
                company_id=self.env.company.id,
                reason_json=self._audit_json(
                    {
                        "provider": self.provider,
                        "model": self.model,
                        "result": result,
                        "status": "success",
                    }
                ),
            )
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("AI Configuration"),
                    "message": result,
                    "type": "success",
                    "sticky": False,
                },
            }
        except Exception as error:
            message = _("Failed: %s") % error
            self.write({"test_result": message})
            self._log_audit_event(
                "ai_connection_test",
                "AI connection test failed",
                company_id=self.env.company.id,
                reason_json=self._audit_json(
                    {
                        "provider": self.provider,
                        "model": self.model,
                        "result": message,
                        "status": "failed",
                    }
                ),
            )
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("AI Configuration"),
                    "message": message,
                    "type": "danger",
                    "sticky": True,
                },
            }


class WmsAiPriorityWhatIf(models.TransientModel):
    # Simulation wizard that compares the current scoring order against a
    # proposed policy draft.
    _name = "wms.ai.priority.whatif"
    _description = "WMS AI What-If Simulator"
    _inherit = "wms.ai.copilot.mixin"

    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)
    warehouse_id = fields.Many2one("stock.warehouse")
    prompt_input = fields.Text(required=True, string="Simulation Prompt")
    proposal_json = fields.Text(readonly=True)
    simulation_summary = fields.Text(readonly=True)
    response_status = fields.Selection(
        [("draft", "Draft"), ("generated", "Generated"), ("fallback", "Fallback Simulation")],
        default="draft",
        readonly=True,
    )

    def action_open_ai_configuration(self):
        # If the provider is not configured, the wizard sends the user directly
        # to the setup form.
        self.ensure_one()
        wizard = self.env["wms.ai.priority.ai.config.wizard"].create({})
        return {
            "type": "ir.actions.act_window",
            "name": "AI Configuration",
            "res_model": "wms.ai.priority.ai.config.wizard",
            "view_mode": "form",
            "view_id": self.env.ref("odoo_picking_priority.view_wms_ai_priority_ai_config_wizard_form").id,
            "target": "new",
            "res_id": wizard.id,
        }

    def action_run_simulation(self):
        # Build a proposed policy, simulate the score movement it would cause,
        # and then ask the AI provider to explain the operational impact.
        for wizard in self:
            try:
                proposal = self._build_policy_proposal(wizard.prompt_input, "Simulation Draft")
                pickings = wizard._get_simulation_pickings()
                impact_payload = wizard._build_simulation_payload(pickings, proposal)
                summary = wizard._build_fallback_simulation_summary(impact_payload)
                status = "fallback"
                if not self._is_ai_configured():
                    return wizard.action_open_ai_configuration()
                if self._is_ai_configured():
                    try:
                        system_prompt = """
You are a warehouse what-if simulator.
Explain the likely operational impact of the proposed policy change.
Focus on rank movements, SLA risk, ready-to-ship pickings, and blocked items.
Keep the response concise and advisory.
""".strip()
                        user_prompt = "Simulation payload:\n%s" % self._safe_json(impact_payload)
                        summary = self._call_ai_provider(system_prompt, user_prompt, temperature=0.2)
                        status = "generated"
                    except Exception as error:
                        _logger.exception("What-if simulation AI failed")
                        raise UserError(_("What-if simulation AI failed: %s") % error) from error
                wizard.write(
                    {
                        "proposal_json": self._safe_json(proposal),
                        "simulation_summary": summary,
                        "response_status": status,
                    }
                )
                wizard._log_audit_event(
                    "ai_simulation",
                    "AI simulation %s for %s" % (status, wizard.company_id.display_name or "company"),
                    company_id=wizard.company_id.id,
                    reason_json=wizard._audit_json(
                        {
                            "company_id": wizard.company_id.id,
                            "warehouse_id": wizard.warehouse_id.id if wizard.warehouse_id else False,
                            "prompt_input": wizard.prompt_input,
                            "proposal": proposal,
                            "impact_payload": impact_payload,
                            "simulation_summary": summary,
                            "response_status": status,
                        }
                    ),
                )
            except Exception as error:
                wizard._log_audit_event(
                    "ai_simulation",
                    "AI simulation failed before completion",
                    company_id=wizard.company_id.id,
                    reason_json=wizard._audit_json(
                        {
                            "company_id": wizard.company_id.id,
                            "warehouse_id": wizard.warehouse_id.id if wizard.warehouse_id else False,
                            "prompt_input": wizard.prompt_input,
                            "proposal": locals().get("proposal"),
                            "impact_payload": locals().get("impact_payload"),
                            "error": str(error),
                        }
                    ),
                )
                raise
        return True

    def _get_simulation_pickings(self):
        # Reuse the active queue logic, but cap the sample size so the payload
        # stays small enough for a concise explanation.
        self.ensure_one()
        open_states = ["confirmed", "assigned"]
        if self.company_id.x_ai_include_waiting_pickings:
            open_states.append("waiting")
        domain = [("company_id", "=", self.company_id.id), ("state", "in", open_states)]
        if self.warehouse_id:
            domain.append(("picking_type_id.warehouse_id", "=", self.warehouse_id.id))
        return self.env["stock.picking"].search(
            domain,
            order="x_ai_priority_score desc, x_ai_sla_deadline asc, create_date asc, id asc",
            limit=25,
        )

    def _build_simulation_payload(self, pickings, proposal):
        # Compare current and simulated scores so the summary can explain who
        # would move up or down under the draft policy.
        default_config = self.env["stock.picking"]._build_ai_factor_config_from_weights(proposal["weights_by_context"]["default"])
        company_max_order_value = self.env["stock.picking"]._get_company_max_order_values()
        movers = []
        for picking in pickings:
            factors, _ = picking._get_ai_priority_factors_from_config(default_config, company_max_order_value)
            simulated_score = min(sum(factors.values()), MAX_PRIORITY_SCORE)
            movers.append(
                {
                    "name": picking.name,
                    "current_score": round(picking.x_ai_priority_score, 2),
                    "simulated_score": round(simulated_score, 2),
                    "delta": round(simulated_score - picking.x_ai_priority_score, 2),
                    "current_rank": picking.x_ai_priority_rank,
                    "delay_risk": picking.x_ai_delay_risk,
                    "recommended_action": picking.x_ai_recommended_action,
                }
            )
        movers.sort(key=lambda item: item["delta"], reverse=True)
        return {
            "company": self.company_id.display_name,
            "warehouse": self.warehouse_id.display_name if self.warehouse_id else "All Warehouses",
            "proposed_policy": proposal,
            "largest_upward_moves": movers[:5],
            "largest_downward_moves": sorted(movers, key=lambda item: item["delta"])[:5],
        }

    def _build_fallback_simulation_summary(self, payload):
        # Deterministic summary used when the AI provider is unavailable.
        lines = ["What-if simulation for %s" % payload["warehouse"]]
        if payload["largest_upward_moves"]:
            lines.append(
                "Largest upward moves: %s."
                % ", ".join("%s (%+.2f)" % (item["name"], item["delta"]) for item in payload["largest_upward_moves"])
            )
        if payload["largest_downward_moves"]:
            lines.append(
                "Largest downward moves: %s."
                % ", ".join("%s (%+.2f)" % (item["name"], item["delta"]) for item in payload["largest_downward_moves"])
            )
        lines.append("Review the proposed policy preview before deciding whether to apply a real policy change.")
        return "\n".join(lines)


class WmsAiPrioritySearch(models.TransientModel):
    # Natural-language search wizard that translates a supervisor's question
    # into a safe ORM domain.
    _name = "wms.ai.priority.search"
    _description = "WMS AI Natural Language Search"
    _inherit = "wms.ai.copilot.mixin"

    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)
    warehouse_id = fields.Many2one("stock.warehouse")
    query_text = fields.Text(required=True, string="Natural Language Query")
    search_json = fields.Text(readonly=True, string="Interpreted Filters")
    result_summary = fields.Text(readonly=True)
    result_picking_ids = fields.Many2many("stock.picking", string="Matching Pickings", readonly=True)
    result_count = fields.Integer(readonly=True)

    def action_run_search(self):
        # Interpret the query, execute the safe search, and persist both the
        # filters and the result set for review.
        for wizard in self:
            try:
                interpreted = wizard._interpret_query()
                pickings = wizard._run_safe_search(interpreted)
                summary = wizard._build_search_summary(interpreted, pickings)
                wizard.write(
                    {
                        "search_json": self._safe_json(interpreted),
                        "result_picking_ids": [(6, 0, pickings.ids)],
                        "result_count": len(pickings),
                        "result_summary": summary,
                    }
                )
                wizard._log_audit_event(
                    "ai_search",
                    "AI search executed with %s matches" % len(pickings),
                    company_id=wizard.company_id.id,
                    reason_json=wizard._audit_json(
                        {
                            "company_id": wizard.company_id.id,
                            "warehouse_id": wizard.warehouse_id.id if wizard.warehouse_id else False,
                            "query_text": wizard.query_text,
                            "interpreted_filters": interpreted,
                            "result_count": len(pickings),
                            "result_names": pickings.mapped("name")[:20],
                        }
                    ),
                )
            except Exception as error:
                wizard._log_audit_event(
                    "ai_search",
                    "AI search failed",
                    company_id=wizard.company_id.id,
                    reason_json=wizard._audit_json(
                        {
                            "company_id": wizard.company_id.id,
                            "warehouse_id": wizard.warehouse_id.id if wizard.warehouse_id else False,
                            "query_text": wizard.query_text,
                            "error": str(error),
                        }
                    ),
                )
                raise
        return True

    def _interpret_query(self):
        # Convert a free-form question into a structured search specification,
        # using AI first and keyword heuristics as the fallback.
        self.ensure_one()
        deadline_filter = self._extract_deadline_filter_from_text(self.query_text)
        if self._is_ai_configured():
            try:
                system_prompt = """
You translate warehouse supervisor questions into strict JSON filters.
Supported fields:
- states: confirmed, assigned, waiting
- priority_buckets: critical, high, medium, low
- delay_risks: critical, high, medium, low
- recommended_actions: pick_now, pick_next, expedite_stock, review_override, monitor
- source_channels: marketplace, retail_store, b2b, internal_transfer, store_replenishment, other
- urgency_levels: normal, high, critical
- ready_only: true/false
- manual_override: true/false/null
- deadline_date_from: YYYY-MM-DD or null
- deadline_date_to: YYYY-MM-DD or null
- limit: integer
- sort_by: score_desc, rank_asc, deadline_asc
Return JSON only.
""".strip()
                user_prompt = "Question:\n%s" % self.query_text
                interpreted = self._extract_json_object(self._call_ai_provider(system_prompt, user_prompt, temperature=0.1))
                result = {
                    "states": interpreted.get("states") or ["confirmed", "assigned"],
                    "priority_buckets": interpreted.get("priority_buckets") or [],
                    "delay_risks": interpreted.get("delay_risks") or [],
                    "recommended_actions": interpreted.get("recommended_actions") or [],
                    "source_channels": interpreted.get("source_channels") or [],
                    "urgency_levels": interpreted.get("urgency_levels") or [],
                    "ready_only": bool(interpreted.get("ready_only")),
                    "manual_override": interpreted.get("manual_override"),
                    "deadline_date_from": interpreted.get("deadline_date_from"),
                    "deadline_date_to": interpreted.get("deadline_date_to"),
                    "limit": min(max(int(interpreted.get("limit") or 10), 1), 50),
                    "sort_by": interpreted.get("sort_by") or "score_desc",
                }
                if deadline_filter["deadline_date_from"]:
                    result["deadline_date_from"] = deadline_filter["deadline_date_from"]
                    result["deadline_date_to"] = deadline_filter["deadline_date_to"]
                    result["sort_by"] = "deadline_asc"
                return result
            except Exception as error:
                _logger.exception("Natural language search AI failed")
                raise UserError(_("Natural language search AI failed: %s") % error) from error
        text = (self.query_text or "").lower()
        return {
            "states": ["confirmed", "assigned"] if "waiting" not in text else ["confirmed", "assigned", "waiting"],
            "priority_buckets": ["critical", "high"] if "at-risk" in text or "urgent" in text else [],
            "delay_risks": ["critical", "high"] if "miss sla" in text or "at-risk" in text else [],
            "recommended_actions": ["expedite_stock"] if "blocked" in text or "replenishment" in text else [],
            "source_channels": ["marketplace"] if "marketplace" in text else [],
            "urgency_levels": ["critical"] if "critical" in text else [],
            "ready_only": "ready" in text,
            "manual_override": True if "override" in text else None,
            "deadline_date_from": deadline_filter["deadline_date_from"],
            "deadline_date_to": deadline_filter["deadline_date_to"],
            "limit": 10,
            "sort_by": "deadline_asc" if deadline_filter["deadline_date_from"] else "score_desc",
        }

    def _extract_deadline_filter_from_text(self, text):
        # Pull any explicit date references out of the query so date filters are
        # applied even if the model misses them.
        text = (text or "").strip().lower()
        today = fields.Date.context_today(self)
        target_date = None

        if "yesterday" in text:
            target_date = today - timedelta(days=1)
        elif "today" in text:
            target_date = today
        elif "tomorrow" in text:
            target_date = today + timedelta(days=1)
        else:
            month_names = (
                "january|february|march|april|may|june|july|august|september|october|november|december"
            )
            patterns = [
                rf"\b({month_names})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s*(\d{{4}}))?\b",
                rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_names})(?:,?\s*(\d{{4}}))?\b",
            ]
            for pattern in patterns:
                match = re.search(pattern, text, re.I)
                if not match:
                    continue
                part1, part2, year_text = match.groups()
                if re.match(r"^\d{1,2}$", part1):
                    day_text, month_name = part1, part2
                else:
                    month_name, day_text = part1, part2
                parsed_year = int(year_text) if year_text else today.year
                parsed_text = "%s %s %s" % (month_name, day_text, parsed_year)
                try:
                    target_date = datetime.strptime(parsed_text, "%B %d %Y").date()
                    break
                except ValueError:
                    target_date = None

        if not target_date:
            return {"deadline_date_from": False, "deadline_date_to": False}
        return {
            "deadline_date_from": fields.Date.to_string(target_date),
            "deadline_date_to": fields.Date.to_string(target_date),
        }

    def _run_safe_search(self, interpreted):
        # Turn the interpreted filter object into a constrained ORM domain.
        self.ensure_one()
        domain = [("company_id", "=", self.company_id.id), ("state", "in", interpreted["states"])]
        if self.warehouse_id:
            domain.append(("picking_type_id.warehouse_id", "=", self.warehouse_id.id))
        if interpreted["priority_buckets"]:
            domain.append(("x_ai_priority_bucket", "in", interpreted["priority_buckets"]))
        if interpreted["delay_risks"]:
            domain.append(("x_ai_delay_risk", "in", interpreted["delay_risks"]))
        if interpreted["recommended_actions"]:
            domain.append(("x_ai_recommended_action", "in", interpreted["recommended_actions"]))
        if interpreted["source_channels"]:
            domain.append(("x_ai_source_channel", "in", interpreted["source_channels"]))
        if interpreted["urgency_levels"]:
            domain.append(("x_ai_urgency_level", "in", interpreted["urgency_levels"]))
        if interpreted["ready_only"]:
            domain.append(("x_ai_availability_ratio", ">=", 99.99))
        if interpreted["manual_override"] is True:
            domain.append(("x_ai_manual_override", "=", True))
        elif interpreted["manual_override"] is False:
            domain.append(("x_ai_manual_override", "=", False))
        if interpreted.get("deadline_date_from"):
            start_dt_utc, _ = self._get_utc_day_bounds(interpreted["deadline_date_from"])
            domain.append(("x_effective_priority_deadline", ">=", fields.Datetime.to_string(start_dt_utc)))
        if interpreted.get("deadline_date_to"):
            _, end_dt_utc = self._get_utc_day_bounds(interpreted["deadline_date_to"])
            domain.append(("x_effective_priority_deadline", "<", fields.Datetime.to_string(end_dt_utc)))
        order_map = {
            "score_desc": "x_ai_priority_score desc, x_ai_sla_deadline asc, create_date asc, id asc",
            "rank_asc": "x_ai_priority_rank asc, x_ai_priority_score desc, x_ai_sla_deadline asc, create_date asc, id asc",
            "deadline_asc": "x_ai_sla_deadline asc, x_ai_priority_score desc, create_date asc, id asc",
        }
        return self.env["stock.picking"].search(
            domain,
            order=order_map.get(interpreted["sort_by"], order_map["score_desc"]),
            limit=interpreted["limit"],
        )

    def _build_search_summary(self, interpreted, pickings):
        # Summarize the matching records in plain language instead of exposing
        # only a raw count.
        names = ", ".join(pickings.mapped("name")[:8]) if pickings else "No pickings matched."
        return "Found %s pickings using safe filters.\nTop matches: %s\nSort: %s" % (
            len(pickings),
            names,
            interpreted["sort_by"],
        )

    def _get_utc_day_bounds(self, date_value):
        target_date = fields.Date.to_date(date_value)
        timezone_name = self.env.context.get("tz") or self.env.user.tz or "UTC"
        timezone = pytz.timezone(timezone_name)
        local_start = timezone.localize(datetime.combine(target_date, time.min))
        local_end = timezone.localize(datetime.combine(target_date + timedelta(days=1), time.min))
        utc_start = local_start.astimezone(pytz.UTC).replace(tzinfo=None)
        utc_end = local_end.astimezone(pytz.UTC).replace(tzinfo=None)
        return utc_start, utc_end


class StockPicking(models.Model):
    _inherit = "stock.picking"

    @api.model
    def _audit_json(self, value):
        if value in (None, False):
            return False
        if isinstance(value, str):
            return value
        return json.dumps(value, indent=2, default=str)

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

    def action_open_ai_picking_assistant(self):
        self.ensure_one()
        self._log_audit_event(
            "ai_action",
            "Opened Ask AI assistant",
            company_id=self.company_id.id,
            picking_id=self.id,
            reason_json=self._audit_json(
                {
                    "picking_name": self.name,
                    "picking_id": self.id,
                }
            ),
        )
        wizard = self.env["wms.ai.picking.assistant"].create(
            {
                "picking_id": self.id,
                "question": "Why is this picking ranked here, and what should the warehouse supervisor do next?",
            }
        )
        return {
            "type": "ir.actions.act_window",
            "name": "Ask AI",
            "res_model": "wms.ai.picking.assistant",
            "view_mode": "form",
            "target": "new",
            "res_id": wizard.id,
        }

    @api.model
    def _build_ai_factor_config_from_weights(self, weights):
        return {
            factor_name: {
                "max": max(float(weights.get(factor_name, 0.0)), 0.0),
                "enabled": float(weights.get(factor_name, 0.0)) > 0.0,
            }
            for factor_name in DEFAULT_FACTOR_CONFIG
        }

    def _get_ai_priority_factors_from_config(self, config, company_max_order_value):
        self.ensure_one()
        max_order_value = company_max_order_value.get(self.company_id.id, 0.0)
        order_value = self._get_ai_order_value()
        _, _, availability_ratio = self._get_ai_availability_metrics()
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
            {"factor": factor_name, "score": details["score"], "label": details["label"]}
            for factor_name, details in sorted(factor_details.items(), key=lambda item: item[1]["score"], reverse=True)
            if details["score"] > 0
        ]
        return factors, reason_lines

    def _get_ai_assistant_context_payload(self):
        self.ensure_one()
        total_demand, total_reserved, availability_ratio = self._get_ai_availability_metrics()
        reason_payload = {}
        if self.x_ai_priority_reason_json:
            try:
                reason_payload = json.loads(self.x_ai_priority_reason_json)
            except json.JSONDecodeError:
                reason_payload = {"raw_reason": self.x_ai_priority_reason_json}
        return {
            "picking_name": self.name,
            "priority_score": self.x_ai_priority_score,
            "priority_bucket": self.x_ai_priority_bucket,
            "priority_rank": self.x_ai_priority_rank,
            "manual_rank": self.x_manual_priority_rank_display,
            "final_rank": self.x_display_priority_rank,
            "delay_risk": self.x_ai_delay_risk,
            "delay_risk_reason": self.x_ai_delay_risk_reason,
            "recommended_action": self.x_ai_recommended_action,
            "priority_reason": self.x_ai_priority_reason,
            "urgency_level": self.x_ai_urgency_level,
            "source_channel": self.x_ai_source_channel,
            "sla_deadline": str(self._get_ai_scoring_deadline() if hasattr(self, "_get_ai_scoring_deadline") else (self.x_ai_sla_deadline or self.x_ai_customer_sla_date or "")),
            "dispatch_cutoff": str(self.x_ai_dispatch_cutoff or ""),
            "availability_ratio": round(availability_ratio * 100, 2),
            "total_demand_qty": total_demand,
            "total_reserved_qty": total_reserved,
            "manual_override": self.x_ai_manual_override,
            "override_reason": self.x_ai_override_reason,
            "reason_payload": reason_payload,
        }

    def _build_ai_assistant_fallback_answer(self, question):
        self.ensure_one()
        parts = [
            "This picking currently sits at score %.2f with delay risk %s."
            % (self.x_ai_priority_score, self.x_ai_delay_risk or "low"),
            self.x_ai_priority_reason or "No detailed priority explanation is available yet.",
        ]
        if self.x_ai_delay_risk_reason:
            parts.append(self.x_ai_delay_risk_reason)
        if question and "replenishment" in question.lower() and self.x_ai_recommended_action == "expedite_stock":
            parts.append("Recommended next step: expedite stock or replenishment before releasing the pick.")
        elif self.x_ai_recommended_action:
            action_label = dict(self._fields["x_ai_recommended_action"].selection).get(
                self.x_ai_recommended_action,
                self.x_ai_recommended_action,
            )
            parts.append("Recommended next step: %s." % action_label)
        return "\n".join(parts)
