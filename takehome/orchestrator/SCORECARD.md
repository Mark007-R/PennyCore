# Scoring Rubric — Autonomy-Gated Agent Orchestrator

> **Internal document. Remove before distributing to candidates.**

## Overview

- **Design: 70%** | **Implementation: 30%**
- Each criterion: **0** (missing), **1** (partial/basic), **2** (solid/well-thought-out)
- Maximum core score: 44 points (22 criteria × 2)

---

## Design (70%) — 14 criteria, max 28 points

### System Architecture (6 criteria)

| # | Criterion | 0 | 1 | 2 |
|---|-----------|---|---|---|
| D1 | **Event-action separation** | Events and actions are the same concept or conflated | Separate types but tightly coupled | Clean separation — events are immutable inputs, actions are outputs of planning |
| D2 | **Layered orchestration** | Monolithic handler that does everything | Some separation of concerns | Clear layers: ingestion → planning → policy gating → execution/queuing |
| D3 | **State management** | No persistent state between calls | Basic in-memory dict | Thoughtful state model (action store, event log, or explicit abstraction) |
| D4 | **Tenant isolation** | Single global namespace | Tenant ID on records | Explicit per-tenant state boundaries; no cross-tenant leakage possible |
| D5 | **Extensibility** | Hard-coded event/action types everywhere | Some configurability | New event/action types can be added without changing core logic |
| D6 | **Error boundaries** | Exceptions propagate uncaught from LLM/internals | Some try/except | Graceful degradation — LLM failures don't crash the system; invalid inputs rejected cleanly |

### Policy Engine (4 criteria)

| # | Criterion | 0 | 1 | 2 |
|---|-----------|---|---|---|
| D7 | **Basic gating works** | No policy enforcement (all auto or all pending) | Binary auto/manual per action type | Policies correctly gate actions per tenant per action type |
| D8 | **Policy expressiveness** | Only simple string mapping | Supports defaults or wildcards | Supports conditional rules, thresholds, or context-aware policies |
| D9 | **Default policy** | Unconfigured action types crash or silently auto-approve | Some fallback behavior | Explicit default handling (e.g., approval_required for unknown types) |
| D10 | **Policy isolation** | Shared policies across tenants | Per-tenant but with leakage risk | Per-tenant policies with clean configuration API |

### Audit & Observability (4 criteria)

| # | Criterion | 0 | 1 | 2 |
|---|-----------|---|---|---|
| D11 | **Action audit trail** | No history on actions | Timestamps on actions | Full status history with timestamps and transitions |
| D12 | **LLM reasoning capture** | LLM output discarded after planning | Reasoning stored as string on action | Reasoning plus model/prompt metadata stored and retrievable |
| D13 | **Idempotency** | Duplicate events produce duplicate actions | Some dedup attempt | Event dedup by ID with clear idempotency guarantee |
| D14 | **Status state machine** | Free-form string status with no rules | Named statuses but transitions not enforced | Explicit valid transitions; invalid transitions raise errors |

---

## Implementation (30%) — 8 criteria, max 16 points

### Code Quality (4 criteria)

| # | Criterion | 0 | 1 | 2 |
|---|-----------|---|---|---|
| I1 | **Type annotations** | No type hints | Partial type hints | Comprehensive annotations (dataclasses/Pydantic, typed returns) |
| I2 | **Code organization** | Single file, everything mixed | Multiple files but unclear boundaries | Clean module structure with clear responsibility per file |
| I3 | **Naming and readability** | Unclear variable/function names | Adequate naming | Self-documenting code; domain vocabulary used consistently |
| I4 | **Dependency management** | requirements.txt broken or missing | Basic requirements | Pinned versions, only necessary dependencies |

### Robustness (4 criteria)

| # | Criterion | 0 | 1 | 2 |
|---|-----------|---|---|---|
| I5 | **LLM integration quality** | API key hardcoded; no error handling | Basic OpenAI call with error handling | Structured output; retry logic; graceful fallback on failure |
| I6 | **Input validation** | No validation of events/policies | Some field checks | Events and configs validated with clear error messages |
| I7 | **evaluate.py pass rate** | 0–2 scenarios pass | 3–4 scenarios pass | 5–6 scenarios pass |
| I8 | **Edge case handling** | Crashes on unexpected input | Handles some edge cases | Empty events, missing fields, concurrent operations handled |

---

## Bonus (add to total, not required)

| # | Criterion | Points |
|---|-----------|--------|
| B1 | **DESIGN.md exists** with architecture explanation | +1 |
| B2 | **Diagram included** (ASCII, Mermaid, or image file) | +2 |
| B3 | **Creative extensions** (action batching, priority queues, escalation, webhooks, streaming) | +1 to +3 |
| B4 | **Own test suite** beyond evaluate.py | +1 |
| B5 | **Structured LLM output** (tool use, JSON mode, function calling) | +1 |

---

## Scoring Guide

| Score (out of 44 core) | Assessment |
|------------------------|------------|
| 36+ | **Strong hire** — deep design thinking, clean implementation |
| 28–35 | **Hire** — solid architecture with minor gaps |
| 20–27 | **Borderline** — some good ideas but significant gaps |
| <20 | **No hire** — insufficient design depth or broken implementation |

---

## Red Flags (automatic downgrade)

- [ ] Solution is clearly fully AI-generated with no evidence of original thinking
- [ ] Modified evaluate.py to make tests pass
- [ ] No real LLM integration (hardcoded responses to pass scenarios)
- [ ] Policies have no observable effect (all actions always auto-approved or always pending)
- [ ] Protocol methods are not async

## Green Flags (consider upgrading)

- [ ] DESIGN.md shows genuine tradeoff reasoning, not just "I chose X"
- [ ] Policy engine is composable (rule combinators, priority, context-aware conditions)
- [ ] Demonstrates awareness of production concerns (rate limiting, cost, latency, persistence)
- [ ] Clean separation would let you swap the LLM provider trivially
- [ ] Status state machine is explicitly modeled (enum, transition table, or similar)
