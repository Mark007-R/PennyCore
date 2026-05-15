# Phase-3 Benchmark Dataset

200 `(customer_history, query, ground_truth)` pairs. Used by the
context-engine comparison study (Days 13-15) to score five retrieval
strategies on the same workload.

## Files

| File | Lines | Purpose |
|------|-------|---------|
| `customer_histories.jsonl` | 200 | One customer's full history per line |
| `queries.jsonl` | 200 | One pair per line; references `customer_id` |
| `manifest.json` | — | Build seed, counts, sha256 of the artifacts |
| `build_dataset.py` | — | Deterministic builder (re-run with `--seed 42` reproduces) |

The dataset directory was originally specified as a folder of 200
per-customer JSON files. We chose JSONL instead because (a) it gives
clean PR diffs when the dataset evolves, (b) a single file load is the
common case for benchmark scripts, and (c) the loader exposes the same
per-customer view via `iter_pairs()`. The directory still lives under
`benchmarks/data/` as the SKILL prescribes.

## Schemas

### `customer_histories.jsonl`

```jsonc
{
  "customer_id": "cust_b001",
  "tenant_id": "tenant_acme_bank",
  "channel_messages": [
    {
      "channel": "chat" | "email" | "sms" | "voice",
      "sender": "Jane Doe" | "Loan Officer Sam Patel" | "customer" | "agent",
      "content": "...",
      "timestamp": "2026-04-15T09:32:00+00:00",
      "metadata": {"subject": "...", "from_phone": "...", "source_dialogue": "..."}
    }
  ],
  "prior_actions": [
    {
      "action_type": "send_checklist",
      "details": {"items": ["w2", "pay_stubs"]},
      "timestamp": "2026-04-15T09:00:00+00:00"
    }
  ],
  "source": "banking_template" | "multiwoz",
  "source_ids": ["template_00_variant_0"] | ["PMUL4398.json", "MUL0123.json"]
}
```

`source_ids` carries the provenance back to the input artifact:
- For banking templates: `template_<NN>_variant_<V>`.
- For MultiWOZ-derived: the original `dialogue_id` strings, ordered
  oldest-first. Long histories may concatenate 10+ source dialogues.

### `queries.jsonl`

```jsonc
{
  "pair_id": "pair_001",
  "customer_id": "cust_b001",
  "tenant_id": "tenant_acme_bank",
  "query": "Can you confirm the mortgage rate I was quoted?",
  "ground_truth": "The rate quoted was 6.25% locked for 30 days...",
  "history_message_count": 14,
  "history_bucket": "short" | "medium" | "long" | "very_long",
  "source": "banking_template" | "multiwoz",
  "intent": "rate_lookup" | "checklist_followup" | ...,
  "tags": ["mortgage", "rate"]
}
```

`pair_id` and `customer_id` are 1:1 in this dataset (one query per
customer history). That keeps the comparison study clean: every
strategy retrieves from the same fixed history per query, so any
quality delta is attributable to retrieval choice, not history mix.

## Length distribution

| Bucket | Range | Target | Actual |
|--------|-------|--------|--------|
| short | 10-30 messages | 100 | 100 |
| medium | 31-100 messages | 60 | 60 |
| long | 101-300 messages | 30 | 30 |
| very_long | 301-500 messages | 10 | 10 |

The distribution is intentional. Short-history pairs are where naive
"dump everything" wins (cheap, fast, accurate). Long and very-long
pairs are where retrieval pays off — they're underrepresented by count
but they drive the cost-per-quality story for the whole study.

## Sources

### Banking templates (50 pairs)

10 mortgage/loan scenario templates × 5 variants each. Each variant
modulates channel mix (chat-only, email-only, mixed, SMS-heavy,
cross-channel handoff) and history length (10, 14, 18, 22, 28 messages).
All names are clearly fake (Jane Doe, Marcus Bell, Sofia Ortiz, Theo
Fairfax, Lin Zhao) per the SKILL's "no real PII patterns" rule.
Tenants alternate between `tenant_acme_bank` and `tenant_jefferson_credit`
so multi-tenant isolation tests in Phase 4 can use this dataset.

Templates:
1. `rate_lookup` — borrower asks about a previously quoted rate
2. `checklist_followup` — borrower asks which docs are still missing
3. `status_check` — application stage / underwriting timeline
4. `refi_question` — refinancing math
5. `closing_logistics` — closing date + location
6. `handoff_followup` — multi-channel handoff (chat → email)
7. `preapproval_expiry` — when does the pre-approval letter expire
8. `appraisal_outcome` — appraised value + low-appraisal options
9. `down_payment_breakdown` — down payment + earnest money interaction
10. `pmi_question` — PMI rules + when removable

Banking ground-truths are precise and grounded: a strategy that
correctly retrieves the relevant message gets the answer; a strategy
that doesn't, won't.

### MultiWOZ-derived (150 pairs)

Real public dialogues from the [MultiWOZ 2.2](https://huggingface.co/datasets/multi_woz_v22)
dataset (HuggingFace mirror). Travel/concierge domain (hotels,
restaurants, taxis, attractions). Loaded streaming, sorted by
`dialogue_id` for determinism, filtered to dialogues with ≥3 user turns.

Per pair, we carve:
- Last user turn → **query**
- The system response right after → **ground_truth**
- Every turn before → **history**

Long and very-long buckets concatenate multiple unrelated source
dialogues (older first; the primary dialogue stays newest so its
context still anchors the answer). This simulates an active
multi-channel customer with many prior conversations, which the
benchmark's retrieval strategies must filter through.

**Known caveat:** some MultiWOZ ground-truths are conversational
closers ("Thank you, you're welcome, goodbye!") rather than
information-bearing answers. We keep them because the dataset's role
in Phase 3 is COMPARING strategies on the same workload, not
absolute-quality benchmarking. The LLM-as-judge in Day 15 scores
faithfulness vs ground truth, which still differentiates
strategies even on conversational-closer pairs (a strategy that
hallucinates new content scores worse than one that politely echoes).

License: MultiWOZ 2.2 is distributed under an Apache-style license
(see the HuggingFace dataset card for the canonical text). We use it
only for academic-style benchmarking; no MultiWOZ content is shipped
to any external service in mock mode.

## Reproducibility

```bash
python benchmarks/data/build_dataset.py --seed 42
```

This regenerates `customer_histories.jsonl`, `queries.jsonl`, and
`manifest.json` byte-for-byte (modulo `manifest.build_timestamp_utc`).
The committed `manifest.artifact_sha256` field can be cross-checked:

```bash
python -c "import hashlib, pathlib; \
d = pathlib.Path('benchmarks/data'); \
print(hashlib.sha256(\
  (d/'customer_histories.jsonl').read_bytes() + \
  (d/'queries.jsonl').read_bytes()\
).hexdigest())"
```

If that hex doesn't match `manifest.artifact_sha256`, somebody
modified the artifacts by hand — re-run the builder to restore them
or bump `manifest.schema_version` if the change was intentional.

## How the benchmark scripts use this

```python
from benchmarks.dataset_loader import load_pairs

for pair in load_pairs():
    history = pair.history          # CustomerHistory dataclass
    query = pair.query              # str
    truth = pair.ground_truth       # str
    bucket = pair.history_bucket    # short|medium|long|very_long

    brief = strategy.assemble(history=history, query=query,
                              token_budget=8_000)
    answer = llm.answer(brief=brief, query=query)
    score = judge.score(answer=answer, ground_truth=truth)
    record(pair_id=pair.pair_id, strategy=strategy.name,
           bucket=bucket, score=score, ...)
```

`load_pairs()` joins `queries.jsonl` to `customer_histories.jsonl` by
`customer_id` and yields one `BenchmarkPair` dataclass per row.
