# Scoring Rubric — AI Agent Context & Memory System

> **Internal document. Remove before distributing to candidates.**

## Overview

- **Design: 70%** | **Implementation: 30%**
- Each criterion: **0** (missing), **1** (partial/basic), **2** (solid/well-thought-out)
- Maximum core score: 42 points (21 criteria × 2)

---

## Design (70%) — 16 criteria, max 32 points

### Memory Architecture (5 criteria)

| # | Criterion | 0 | 1 | 2 |
|---|-----------|---|---|---|
| A1 | **Storage model** | No coherent data model | Dict/list dump | Typed models with indexing by borrower, time, and channel |
| A2 | **Multi-tier or prioritized memory** | Flat list, everything treated equally | Some recency logic | Explicit tiers (working/short-term/long-term) or ranked retrieval with explanation |
| A3 | **Summarization or compression** | None — raw messages or simple truncation | Basic truncation (first/last N) | LLM-based summarization, fact extraction, or progressive compression |
| A4 | **Staleness / decay** | Not addressed | Mentioned but not implemented | Time-based weighting, TTL, or decay scoring in retrieval |
| A5 | **Extensibility** | Hardcoded channel types and metadata handling | Somewhat flexible | New channels, message types, or metadata handled without rewriting core |

### Context Assembly (5 criteria)

| # | Criterion | 0 | 1 | 2 |
|---|-----------|---|---|---|
| B1 | **Budget enforcement** | Context regularly exceeds budget | Usually within budget | Consistently within budget with margin management |
| B2 | **Token estimation** | No estimation (just character count) | ~4 chars/token approximation | tiktoken or model-aware counting |
| B3 | **Prioritization strategy** | Random or purely chronological | Recency-based only | Multi-signal ranking (recency + relevance + importance) with explanation |
| B4 | **Context structure** | Raw message dump sent to LLM | Basic formatting (timestamps, labels) | Structured sections (summary, recent messages, actions, key facts) |
| B5 | **Budget allocation** | No allocation — first-come-first-served | Implicit via ordering | Explicit budget partitioning across categories with rationale |

### Cross-Channel Handling (3 criteria)

| # | Criterion | 0 | 1 | 2 |
|---|-----------|---|---|---|
| C1 | **Unified borrower view** | Channels siloed — separate histories | Merged but channel info lost | Unified timeline with channel metadata preserved |
| C2 | **Channel-aware context** | No channel information in assembled context | Channel stored but not surfaced | Channel provenance visible in assembled context (e.g., "[email]", "[chat]") |
| C3 | **Channel-specific metadata** | Email subjects, attachments, etc. ignored | Stored but not used | Stored and surfaced in context assembly when relevant |

### Action Memory (3 criteria)

| # | Criterion | 0 | 1 | 2 |
|---|-----------|---|---|---|
| D1 | **Action storage** | Actions not stored or barely stored | Stored minimally (type only) | Stored with full detail, timestamp, and queryable by type |
| D2 | **Actions in context** | Prior actions not included in assembled context | Included as raw list | Formatted meaningfully (e.g., "Already sent document checklist on...") |
| D3 | **Deduplication awareness** | No acknowledgment that repeating actions is a problem | Mentioned in design doc | Implemented or clearly designed with dedup rules |

---

## Implementation (30%) — 5 criteria, max 10 points

| # | Criterion | 0 | 1 | 2 |
|---|-----------|---|---|---|
| E1 | **evaluate.py pass rate** (non-LLM scenarios) | 0–2 pass | 3–4 pass | All 5 pass |
| E2 | **LLM scenario passes** | Fails or skipped | Passes with weak response | Passes cleanly |
| E3 | **Code quality** | Messy, no types, no structure | Adequate organization and naming | Clean with types, docstrings, clear module boundaries |
| E4 | **Error handling** | Crashes on empty history, unknown borrower, zero budget | Some guards | Robust handling with clear defined behavior for edge cases |
| E5 | **LLM usage quality** | No LLM usage beyond evaluate.py's test | Basic completion call somewhere | Thoughtful integration (summarization, fact extraction, importance scoring) |

---

## Bonus (add to total, not required)

| # | Criterion | Points |
|---|-----------|--------|
| F1 | **DESIGN.md exists** with clear architecture explanation | +2 |
| F2 | **Diagram included** (ASCII, Mermaid, or image file) | +2 |
| F3 | **Creative extensions** (embedding-based retrieval, conversation threading, importance scoring, semantic search) | +2 |
| F4 | **Cost awareness** — discusses or tracks token costs, optimizes for budget | +2 |

---

## Scoring Guide

| Score (out of 42 core) | Assessment |
|------------------------|------------|
| 34+ | **Strong hire** — deep design thinking, clean implementation |
| 26–33 | **Hire** — solid architecture with minor gaps |
| 18–25 | **Borderline** — some good ideas but significant gaps |
| <18 | **No hire** — insufficient design depth or broken implementation |

---

## Red Flags (automatic downgrade)

- [ ] Solution is clearly fully AI-generated with no evidence of original thinking
- [ ] Modified evaluate.py to make tests pass
- [ ] Context assembly ignores the token budget entirely
- [ ] Channels are completely siloed (no cross-channel awareness at all)
- [ ] No evidence of running the code (syntax errors, import failures on basic run)

## Green Flags (consider upgrading)

- [ ] DESIGN.md shows genuine tradeoff reasoning ("I chose X over Y because...")
- [ ] Uses LLM for summarization/compression within the memory layer itself
- [ ] Implements embedding-based semantic retrieval for context assembly
- [ ] Discusses production concerns (persistence, concurrency, cost at scale)
- [ ] Clean separation between storage, retrieval, and context assembly layers
- [ ] Token budget allocation is explicitly reasoned about, not just "fill until full"
