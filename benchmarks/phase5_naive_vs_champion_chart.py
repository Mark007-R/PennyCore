"""Render the Day-27 cost-by-bucket comparison chart.

Reads ``results/phase5_naive_vs_champion_context_engine.json`` and writes
``results/phase5_naive_vs_champion_cost_by_bucket.png`` — a grouped bar
chart of USD-per-100q for naive_dump vs hybrid vs recency across the four
history buckets. The very_long bucket is where the gap opens (4x cheaper),
which is the headline the report leads with.
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib.pyplot as plt

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"
IN_PATH = RESULTS_DIR / "phase5_naive_vs_champion_context_engine.json"
OUT_PATH = RESULTS_DIR / "phase5_naive_vs_champion_cost_by_bucket.png"

BUCKET_ORDER = ["short", "medium", "long", "very_long"]
STRATEGY_ORDER = ["naive_dump", "hybrid", "recency"]
STRATEGY_COLORS = {
    "naive_dump": "#c0392b",  # red — the baseline you want to beat
    "hybrid": "#27ae60",  # green — the champion
    "recency": "#7f8c8d",  # gray — reference
}
STRATEGY_LABELS = {
    "naive_dump": "Naive (dump everything, 50K budget)",
    "hybrid": "Hybrid (champion, 8K budget)",
    "recency": "Recency-only (8K budget)",
}


def main() -> None:
    data = json.loads(IN_PATH.read_text(encoding="utf-8"))
    per_strategy = data["per_strategy_summary"]
    judge_mode = data["judge_mode"]
    model_label = data["model_label"]

    fig, (ax_cost, ax_tokens) = plt.subplots(1, 2, figsize=(13, 5))
    n_strategies = len(STRATEGY_ORDER)
    bar_w = 0.25
    x_positions = list(range(len(BUCKET_ORDER)))

    for i, sname in enumerate(STRATEGY_ORDER):
        costs = [
            per_strategy[sname]["by_bucket"][b]["usd_per_100q"]
            for b in BUCKET_ORDER
        ]
        tokens = [
            per_strategy[sname]["by_bucket"][b]["avg_brief_tokens"]
            for b in BUCKET_ORDER
        ]
        offsets = [x + (i - (n_strategies - 1) / 2) * bar_w for x in x_positions]
        ax_cost.bar(
            offsets,
            costs,
            width=bar_w,
            color=STRATEGY_COLORS[sname],
            label=STRATEGY_LABELS[sname],
        )
        ax_tokens.bar(
            offsets,
            tokens,
            width=bar_w,
            color=STRATEGY_COLORS[sname],
            label=STRATEGY_LABELS[sname],
        )

    for ax, ylabel, title in (
        (ax_cost, "USD per 100 queries", "Cost frontier"),
        (ax_tokens, "Avg brief tokens", "Brief size"),
    ):
        ax.set_xticks(x_positions)
        ax.set_xticklabels(
            [
                f"{b}\n(n={per_strategy['hybrid']['by_bucket'][b]['n_pairs']})"
                for b in BUCKET_ORDER
            ]
        )
        ax.set_xlabel("History bucket")
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        ax.grid(axis="y", linestyle=":", alpha=0.4)

    ax_cost.legend(loc="upper left", fontsize=8)
    fig.suptitle(
        "Day 27 — Naive baseline vs hybrid champion (context-engine)\n"
        f"Cost projected at {model_label} pricing; judge mode = {judge_mode}",
        fontsize=11,
    )
    plt.tight_layout()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(OUT_PATH, dpi=150, bbox_inches="tight")
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
