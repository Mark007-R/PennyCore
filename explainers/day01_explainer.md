# Day 01 — Plain English Explainer

**Date:** 2026-05-04
**Phase:** 1 — Foundation
**Today in one sentence:** Set up the empty project the right way — folders, secrets template, and the official grading file from the take-home test, all in their proper places.

## What I built today
Imagine starting a new house. Before you put up walls or pick paint, you
walk the lot, mark where each room will go, run conduit for the wiring, and
make sure there's a separate box for the things you don't want guests to
see (like keys and account numbers). That's Day 1.

Concretely, I created the folder layout for everything that's coming over
the next 35 days — places for the "memory" code, the "decision-maker" code,
the tests, the reports, and a special folder called `takehome/` that holds a
grading script from the company that wrote the original assignment. I'm not
allowed to change that grading script — the whole point is that my work has
to plug into it cleanly. So today it just sits there, untouched, waiting for
Day 7 and Day 10 when I write the adapter code that hooks it up to my system.

I also set up two "secrets" files: `.env.example` (a template that's safe to
share, with fake values) and `.env` (the real one with actual API keys, never
shared, marked invisible to git). One pair lives at the project root, and a
separate pair lives next to each take-home grading script — because the main
system uses Anthropic's Claude but the grading script requires an OpenAI-style
API, and I don't want those wires to cross.

## Why I chose [tool/approach] (and what I rejected)

**Why Anthropic Claude for the main system, but Azure OpenAI for the grading
adapter.** I picked Claude as the primary because the project includes a
"who-judges-the-judge" comparison study later, and using one model for both
the work AND the quality judging keeps the comparison clean. But the
take-home spec explicitly says the system has to call an OpenAI-style API,
so for the grading adapter only, I'll use Azure OpenAI. The alternative was
"just use OpenAI everywhere" — simpler, but it would muddy the comparison
study. Splitting the providers via two separate `.env` files keeps both
requirements honest without weird conditional code.

**Why two `.env` files, not one global one.** A single global secrets file
would mean the main system and the grading adapter could accidentally pick
up each other's API keys. By scoping each `.env` to its own folder, the
"adapter always uses Azure" rule becomes a config rule (which is hard to
break) instead of a code rule (which is easy to break in a refactor).

**Why download the take-home grading files via curl, not as a git submodule.**
A submodule would create a permanent dependency on someone else's repo and
clutter `git status` forever. I only need a frozen snapshot of two files that
I'm not allowed to modify anyway. A simple copy is obviously correct and
zero-maintenance.

**Why no Python files yet.** Tempting to drop empty `__init__.py` files into
every package today, but Day 5 creates those modules with actual content. An
empty placeholder today is just churn — it'd get rewritten tomorrow. The
folder scaffold is enough to anchor the layout; code starts Day 5.

## How this fits into the bigger picture
Day 1 is the foundation under everything. The `context_engine/` folder
created today is where Days 5-7 will write the "memory librarian" — the part
that ingests every customer message, links a customer's identity across
email/chat/SMS, and assembles a short, focused brief for the AI before it
replies. The `orchestrator/` folder is where Days 8-10 will build the
"decision-maker" — the part that takes those briefs, asks the AI what to do,
checks the tenant's policy, and either auto-executes the action or queues it
for a human to approve. The `takehome/` adapters (Day 7 and Day 10) are how I
prove all this work also satisfies the original assessment's contract.

Tomorrow I write the system design doc — the architecture diagram and the
key invariants (no double-actions, no tenant data leaks across customers,
every action is traceable). That doc is the contract that the next 33 days
of code has to satisfy.

## Glossary
- **`.env` file**: a small text file that stores secrets like API keys,
  separated from the code. Never shared, never committed to GitHub.
- **Adapter**: a thin wrapper module that translates between two interfaces.
  In our case, between PennyCore's internal API and the take-home grader's
  expected API. No business logic — just plumbing.
- **Multi-tenant**: one system serving multiple customers (Bank A, Bank B,
  Bank C) where each customer's data is invisible to the others.
- **Idempotency**: if the same event is delivered twice (network glitch, a
  retry), the system processes it once. Duplicate events should never cause
  duplicate actions (don't text the customer twice about the same thing).

## What I'm doing tomorrow
Writing the system design doc — the architecture diagram and the rules the
whole system has to obey (no double-actions, no data leaks between tenants,
every action traceable to its trigger).

## LLM mode today
mock — no system code calling any LLM yet. Day 1 is scaffolding only.
