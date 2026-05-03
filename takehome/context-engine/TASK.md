# AI Agent Context & Memory System

## Background

You are building the context and memory layer for an AI assistant that helps
mortgage borrowers through their loan application. The assistant communicates
across **chat, email, SMS, and voice** — sometimes all in the same
application — and maintains ongoing relationships over days or weeks.

Every time the assistant needs to respond, the system assembles relevant
context from prior interactions and sends it to an LLM. The challenge:
conversations can span hundreds of messages across channels, but LLM context
windows have hard token limits. The system must decide what to include, what
to summarize, and what to leave out — while maintaining continuity across
channels and avoiding repeating itself.

## What to Build

Build a Python system that can:

1. **Ingest messages** from multiple channels with metadata
2. **Assemble context** for an LLM call that stays within a given token budget
3. **Maintain continuity** when the same borrower contacts the assistant on
   different channels
4. **Track actions** the assistant has taken so it doesn't repeat itself

Your system must implement the `MemorySystem` protocol defined in
`evaluate.py`. **Read evaluate.py first** — it defines the data types, the
interface contract, and the test scenarios.

## Running

```bash
pip install -r requirements.txt
cp .env.example .env   # add your OpenAI API key
python evaluate.py
```

The evaluator imports your implementation from `memory_system.py`, runs
behavioral scenarios, and prints pass/fail results.

## OpenAI API Key

You have been provided a rate-limited OpenAI API key. One scenario validates
that your assembled context produces a coherent LLM response. You may also
use the API within your implementation (e.g., for summarization). Budget
your calls — the key has limits.

## What We Care About

This is a **design-heavy** assessment. We care more about how you think
about the problem than a polished production system:

- How you structure memory and retrieval
- How you decide what fits in a limited token budget
- How you handle the same person across different channels
- How you prevent the assistant from repeating actions
- What tradeoffs you made and why

## Deliverables

- Your implementation in `memory_system.py` (and any supporting modules)
- A passing `python evaluate.py` run
- **(Bonus)** A `DESIGN.md` explaining your architecture, tradeoffs, and any
  diagrams you want to include

## Constraints

- Python 3.11+
- You may add dependencies to `requirements.txt`
- Do not modify `evaluate.py`
- No Docker or external services (besides the OpenAI API)
