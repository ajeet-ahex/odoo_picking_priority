# Picking Priority Agent

Explainable warehouse picking prioritization for Odoo with a Phase 1 AI copilot layer.

This module follows a simple principle:

- the execution engine is rule-based
- the AI layer is advisory
- users review important changes
- no blind AI warehouse execution happens in Phase 1

## What This Module Does

### Core Rule-Based Features

- Picking priority score
- Priority rank
- Priority bucket
- Recommended action
- Effective priority deadline
- Manual override with audit trail
- Priority scoring logs

### AI Copilot Features

- AI Policy Builder
- Ask AI for a single picking
- AI Queue Summary
- AI What-If Simulator
- AI Warehouse Search

## Product Story

A warehouse supervisor starts the shift and sees many open pickings:

- some are urgent marketplace orders
- some are B2B deliveries
- some are store replenishment transfers
- some are blocked due to low stock
- some are low-pressure future work

Instead of manually guessing what to do first, the system:

1. calculates a deterministic priority score using explainable warehouse rules
2. provides an AI copilot layer to explain, search, summarize, simulate, and propose policy

This gives the user an AI-like experience without losing control or auditability.

## Scoring Logic Overview

The score is based on these factors:

- SLA / Deadline
- Availability
- Manual Urgency
- Channel / Customer SLA
- Downstream Dependency
- Order Value / Business Impact
- Picking Complexity / Quick Win

### Effective Priority Deadline Rule

The module does not blindly use only one date field.

For scoring and urgency evaluation, it derives an `Effective Priority Deadline` from the earliest relevant business date.

Current rule:

- `Effective Priority Deadline = earliest of SLA Deadline, Deadline, and Scheduled Date`

This means:

- if `SLA Deadline` is the earliest, it drives urgency
- if standard `Deadline` is earlier than `Scheduled Date`, the `Deadline` drives urgency
- changing `Scheduled Date` alone will not change the score if another earlier date still exists

Example:

- `Scheduled Date = Mar 26, 4:00 PM`
- `Deadline = Mar 26, 1:42 PM`

Then:

- effective priority deadline remains `Mar 26, 1:42 PM`
- score remains driven by `Deadline`, not by `Scheduled Date`

If you want `Scheduled Date` to affect the score during testing:

- either make `Scheduled Date` the earliest date
- or clear the earlier `SLA Deadline` / `Deadline` values first

Priority buckets:

- `Critical`: 85 and above
- `High`: 70 to 84.99
- `Medium`: 50 to 69.99
- `Low`: below 50

Recommended actions:

- Pick Now
- Pick Next
- Expedite Stock
- Review Override
- Monitor

## What Downstream Dependency Means

Downstream dependency means another process is waiting on this picking.

Examples:

- customer dispatch today depends on this outgoing picking
- store replenishment depends on this internal transfer
- billing may depend on delivery completion
- production may depend on raw material movement

It answers the question:

`If this picking is delayed, what else gets blocked next?`

Current dependency scoring map:

- Blocks same-day customer dispatch: 10
- Blocks store replenishment before opening: 8
- Blocks production continuation: 7
- Blocks invoice / billing cycle: 6
- Internal transfer with limited downstream blocking: 2
- No dependency: 0

## Current UI Flow

### Picking List

Main fields shown:

- Manual Rank
- Priority Rank
- Priority Score
- Priority Bucket
- Recommended Action
- SLA Deadline
- Effective Priority Deadline

### Picking Form

The `AI Priority Insight` tab contains:

- Decision Summary
- AI Insight
- Operational Context
- Score Breakdown
- Manual Override
- Audit Trail

### AI Screens

- `AI Policy Builder`
- `AI Queue Summary`
- `AI What-If Simulator`
- `AI Warehouse Search`
- `Ask AI` on the picking form

## OpenRouter Integration

System parameters:

- `odoo_picking_priority.openrouter_api_key`
- `odoo_picking_priority.openrouter_model`

Optional parameters are already handled in code:

- `odoo_picking_priority.openrouter_base_url`
- `odoo_picking_priority.openrouter_site_url`
- `odoo_picking_priority.openrouter_app_name`

If OpenRouter is unavailable, the module uses deterministic fallback logic where possible.

## End-to-End Test Guide

This section gives a smaller set of realistic test cases that still covers nearly all important behaviors.

## Test Objective

Validate:

- deadline pressure
- full vs partial / zero availability
- marketplace vs B2B vs internal flows
- store replenishment dependency
- invoice dependency
- production dependency
- quick-win complexity behavior
- manual override
- Ask AI
- AI queue summary
- AI what-if simulation
- AI warehouse search
- AI policy builder

## Suggested Setup

### Company and Warehouse

- Company: `Ahex Retail India Pvt Ltd`
- Warehouse: `Bangalore Central Warehouse`
- Address:
  - `#12, Whitefield Industrial Road`
  - `Bangalore, Karnataka 560066`
  - `India`

### Useful Internal Locations

- `WH/Stock`
- `WH/Output`
- `WH/Quality`
- `Store BLR - Replenishment`
- `Production Feed Zone`

### Product Master Data

Create these products for the compact UAT set:

1. `NovaBook 14 Laptop`
- Sale Price: `72000`
- On Hand: `5`

2. `NovaBook X15 Laptop`
- Sale Price: `118000`
- On Hand: `1`

3. `Nova Wireless Mouse`
- Sale Price: `1200`
- On Hand: `80`

4. `Nova Docking Station`
- Sale Price: `8500`
- On Hand: `6`

5. `Nova Thermal Label Printer`
- Sale Price: `14000`
- On Hand: `0`

6. `Nova Monitor 24`
- Sale Price: `12500`
- On Hand: `10`

7. `Battery Module 6-Cell`
- Sale Price: `4500`
- On Hand: `8`

## How to Fill Priority Fields

After confirming an order or transfer and generating the picking:

1. Open the picking
2. Go to `AI Priority Insight`
3. Fill:
   - `SLA Deadline`
   - `Dispatch Cutoff`
   - `Urgency Level`
   - `Source Channel`
4. Save
5. Click `Recalculate Priority`

Why these fields matter:

- `SLA Deadline` drives deadline urgency
- `Dispatch Cutoff` sharpens same-day dispatch urgency
- `Urgency Level` adds manual business escalation
- `Source Channel` changes how the order is treated in channel scoring

Important testing note:

- if you change only `Scheduled Date` and the score does not move, check whether `Deadline` or `SLA Deadline` is still earlier
- the earliest relevant date is what affects the score

## Compact Core Test Cases

Use these 5 quotations and 1 optional B2B quotation. You can attach them to any existing customers.

Assume today is `March 26, 2026`.

### Quotation 1: Fully In Stock, Same-Day Urgent

- Products:
  - `NovaBook 14 Laptop` x `2`
  - `Nova Wireless Mouse` x `2`
- Total: `146400`
- Stock condition: fully available
- Suggested fields:
  - `SLA Deadline`: `2026-03-26 16:00:00`
  - `Dispatch Cutoff`: `2026-03-26 15:00:00`
  - `Urgency Level`: `Critical`
  - `Source Channel`: `Marketplace`

Expected:

- high or critical score
- `Priority Explanation` should clearly say that delaying it risks missing the near-term dispatch window
- `Recommended Action` should typically be `Pick Now`

### Quotation 2: Partial Stock, Same-Day

- Products:
  - `NovaBook X15 Laptop` x `2`
- Total: `236000`
- Stock condition: only `1` available out of `2`
- Suggested fields:
  - `SLA Deadline`: `2026-03-26 18:30:00`
  - `Dispatch Cutoff`: `2026-03-26 17:30:00`
  - `Urgency Level`: `High`
  - `Source Channel`: `Marketplace`

Expected:

- urgency matters, but availability should reduce the score
- `Stock Gap Summary` should show `1.00 available, 1.00 missing`
- `Main reasons` should also mention the stock shortage
- `Recommended Action` should be `Pick Available + Replenish Missing`

### Quotation 3: Completely Out of Stock, Overdue

- Products:
  - `Nova Thermal Label Printer` x `1`
- Total: `14000`
- Stock condition: `0` available
- Suggested fields:
  - `SLA Deadline`: `2026-03-25 17:00:00`
  - `Dispatch Cutoff`: `2026-03-25 16:00:00`
  - `Urgency Level`: `Critical`
  - `Source Channel`: `B2B`

Expected:

- very strong SLA pressure because the deadline is already in the past
- no availability support
- `Recommended Action` should be `Expedite Stock`
- `Priority Explanation` should explain that the order is already overdue and delaying it further increases service risk

### Quotation 4: Future Order, In Stock

- Products:
  - `Nova Monitor 24` x `2`
  - `Nova Wireless Mouse` x `5`
- Total: `31000`
- Stock condition: fully available
- Suggested fields:
  - `SLA Deadline`: `2026-03-28 11:00:00`
  - `Dispatch Cutoff`: `2026-03-28 10:00:00`
  - `Urgency Level`: `Normal`
  - `Source Channel`: `Retail Store`

Expected:

- lower SLA pressure than same-day quotations
- should rank below quotations 1 and 2
- usually `Monitor` or `Pick Next`

### Quotation 5: Production / Dependency Style Transfer

- Products:
  - `Battery Module 6-Cell` x `4`
- Total reference value: `18000`
- Stock condition: fully available
- Suggested fields:
  - `SLA Deadline`: `2026-03-27 09:00:00`
  - `Dispatch Cutoff`: `2026-03-27 08:00:00`
  - `Urgency Level`: `High`
  - `Source Channel`: `Internal Transfer` or production-related flow

Expected:

- should get some dependency-aware importance if linked to production
- should sit above a casual low-pressure transfer

### Optional Quotation 6: High-Value B2B

- Products:
  - `NovaBook 14 Laptop` x `2`
  - `Nova Docking Station` x `2`
- Total: `161000`
- Stock condition: fully available
- Suggested fields:
  - `SLA Deadline`: `2026-03-27 17:00:00`
  - `Dispatch Cutoff`: `2026-03-27 15:00:00`
  - `Urgency Level`: `Normal`
  - `Source Channel`: `B2B`

Expected:

- useful for comparing value-driven B2B work against same-day urgency
- should usually rank below the urgent same-day marketplace order

## Minimal Test Execution Plan

### 1. Basic Scoring

Create Quotations 1 to 5 and recalculate priority.

Validate:

- Quotation 1 ranks above Quotation 4
- Quotation 2 ranks below Quotation 1 because stock is partial
- Quotation 3 is urgent but blocked by zero stock
- Quotation 5 gets a dependency-aware boost if linked to production

### 2. Availability Impact

Compare:

- Quotation 1 vs Quotation 2
- Quotation 2 vs Quotation 3

Validate:

- fully available order should rank better than partial-stock order
- partial-stock order should behave differently from zero-stock order
- stock-constrained quotations should not default to plain `Pick Now`

### 3. Deadline Pressure

Compare:

- Quotation 3 overdue
- Quotation 1 same-day
- Quotation 4 after 2 days

Validate:

- overdue deadline increases SLA pressure
- same-day remains urgent
- future deadline should reduce urgency

### 4. Ask AI

Open Quotation 1 and ask:

- `Why is this picking urgent?`

Open Quotation 2 or 3 and ask:

- `Why is this blocked and what should we do?`

Validate:

- explanation mentions deadline, stock readiness, and suggested action

### 5. AI Queue Summary

With Quotations 1 to 5 active:

- open `AI Queue Summary`
- generate summary

Validate:

- blocked count reflects stock-constrained quotations
- the suggested first wave favors ready and urgent work

### 6. AI What-If Simulator

Use prompt:

`What if marketplace SLA and stock readiness matter most today, while internal transfers should stay below all customer deliveries unless marked critical replenishment?`

Validate:

- proposal changes weights meaningfully
- summary explains likely movers
- live configuration is not changed

### 7. AI Policy Builder

Use prompt:

`For marketplace orders, prioritize dispatch deadline first, then full stock availability. B2B value matters a little. Internal transfers should stay below customer deliveries unless marked critical.`

Validate:

- preview is generated
- weights total 100
- approval is required before apply

### 8. AI Warehouse Search

Use:

1. `Show me marketplace orders blocked by stock`
2. `Show me orders that have a deadline date as tomorrow`
3. `Show me orders that have a deadline date as yesterday`

Validate:

- blocked search includes quotations 2 or 3 when applicable
- date search returns the correct records only

## Expected Relative Ranking

Typical expectation:

1. Quotation 1: urgent, fully ready
2. Quotation 2: urgent, partial stock
3. Quotation 5: dependency-driven transfer if production-linked
4. Quotation 4: future but fully available
5. Quotation 3: overdue but zero stock, operationally blocked

Exact order may vary based on:

- configured weights
- current time at test execution
- reservation state
- dependency linkage
- override status

## Acceptance Criteria

You can consider the module functionally validated if:

- urgent ready marketplace orders rise to the top
- stock-constrained urgent orders do not incorrectly outrank ready urgent orders
- store replenishment and production-linked moves gain dependency-aware scoring
- future low-pressure work stays lower
- AI explanations are consistent with the scoring data
- queue summary reflects the active queue
- what-if simulation does not alter live policy
- policy builder applies changes only after approval
- warehouse search returns accurate filtered results

## Example Prompts

### Policy Builder

`For marketplace orders, prioritize dispatch deadline first, then full stock availability. Internal transfers should stay below customer deliveries unless marked critical.`

### What-If Simulator

`What if marketplace SLA and stock readiness matter most today, while internal transfers should stay below all customer deliveries unless marked critical replenishment?`

### Ask AI

`Why is this picking ranked here, and what should the warehouse supervisor do next?`

### Warehouse Search

`Show me marketplace orders blocked by stock`

`Show me orders that have a deadline date as tomorrow`

`Show me internal transfers with low urgency`

## Current Architecture

Current implementation is Odoo-first:

- Odoo stores the data
- Odoo computes the rule-based score
- Odoo hosts the user interface
- Odoo calls OpenRouter directly for advisory AI features
- Odoo provides fallback logic if AI is unavailable

## Not Yet Split into External Architecture

Future architecture improvements from the broader product vision:

- separate FastAPI orchestration service
- dedicated LLM gateway service
- external analytics / prompt logging service
- external simulation engine

These are not blockers for the current Phase 1 Odoo implementation.
