"""Render the Day-28 orchestrator quality/cost frontier chart.

Reads ``results/phase5_naive_vs_champion_orchestrator.json`` and writes
``results/phase5_naive_vs_champion_orch_frontier.png`` — a two-panel
figure:

* Left: quality/cost scatter — correctness on Y, prod-payload USD/100q
  on X. Declarative sits at (1.0, $0), the free-lunch corner. The naive
  baseline is the (0.54, ~$0.57) point that anchors the comparison.
* Right: per-tenant correctness bar chart — naive_llm fails
  asymmetrically across tenants (over-approves the strict one,
  over-rejects the permissive ones); declarative is flat 100%
  across all three.

The asymmetry is the headline finding for the Phase-5 wrap-up post.
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib.pyplot as plt

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"
IN_PATH = RESULTS_DIR / "phase5_naive_vs_champion_orchestrator.json"
OUT_PATH = RESULTS_DIR / "phase5_naive_vs_champion_orch_frontier.png"

STRATEGY_COLORS = {
    "declarative": "#27ae60",  # green — champion
    "python_rules": "#2980b9",  # blue — runner-up
    "llm_judge": "#8e44ad",  # purple — structured LLM
    "naive_llm": "#c0392b",  # red — baseline
}
STRATEGY_LABELS = {
    "declarative": "Declarative YAML (champion)",
    "python_rules": "Python rules",
    "llm_judge": "LLM-as-judge (structured JSON)",
    "naive_llm": "Naive LLM (free-form prompt)",
}


def main() -> None:
    data = json.loads(IN_PATH.read_text(encoding="utf-8"))

    fig, (ax_frontier, ax_tenant) = plt.subplots(1, 2, figsize=(14, 5.5))

    # --- Frontier scatter (correctness vs cost) -----------------------------
    for row in data["frontier"]:
        sname = row["strategy"]
        ax_frontier.scatter(
            row["prod_payload_usd_per_100"],
            row["correctness"],
            color=STRATEGY_COLORS.get(sname, "#444"),
            s=180,
            edgecolor="black",
            zorder=3,
            label=STRATEGY_LABELS.get(sname, sname),
        )
        # Annotate each point with the strategy short name.
        ax_frontier.annotate(
            sname,
            (row["prod_payload_usd_per_100"], row["correctness"]),
            textcoords="offset points",
            xytext=(8, 6),
            fontsize=9,
        )
    ax_frontier.set_xlabel("Production-payload cost (USD / 100 decisions)")
    ax_frontier.set_ylabel("Decision correctness")
    ax_frontier.set_title("Quality / cost frontier — orchestrator policy engines")
    ax_frontier.set_ylim(0.4, 1.05)
    ax_frontier.grid(linestyle=":", alpha=0.4)
    ax_frontier.legend(loc="lower right", fontsize=8)
    # Highlight the free-lunch corner with a guide line.
    ax_frontier.axhline(1.0, color="#27ae60", linestyle="--", alpha=0.3)
    ax_frontier.axvline(0.0, color="#27ae60", linestyle="--", alpha=0.3)

    # --- Per-tenant correctness bars ----------------------------------------
    per_tenant_naive = data["per_tenant_failure_shape"]["naive_llm"]
    per_tenant_decl = data["per_tenant_failure_shape"]["declarative"]
    tenants = sorted(per_tenant_naive.keys())
    naive_corr = [per_tenant_naive[t]["correctness"] for t in tenants]
    decl_corr = [per_tenant_decl[t]["correctness"] for t in tenants]

    x_pos = list(range(len(tenants)))
    width = 0.36
    ax_tenant.bar(
        [x - width / 2 for x in x_pos],
        naive_corr,
        width=width,
        color=STRATEGY_COLORS["naive_llm"],
        label="Naive LLM",
    )
    ax_tenant.bar(
        [x + width / 2 for x in x_pos],
        decl_corr,
        width=width,
        color=STRATEGY_COLORS["declarative"],
        label="Declarative (champion)",
    )
    # Annotate naive bars with the top-confusion shape so the
    # over-approve / over-reject asymmetry is legible from the chart.
    for i, t in enumerate(tenants):
        conf = per_tenant_naive[t].get("top_confusion") or {}
        if conf:
            ax_tenant.annotate(
                f"top miss:\nexp {conf['expected']}\nobs {conf['observed']}\n(n={conf['count']})",
                xy=(i - width / 2, naive_corr[i]),
                xytext=(i - width / 2, naive_corr[i] - 0.02),
                fontsize=7,
                ha="center",
                va="top",
            )
    ax_tenant.set_xticks(x_pos)
    ax_tenant.set_xticklabels(
        [t.replace("tenant_", "").replace("_", "\n") for t in tenants],
        fontsize=9,
    )
    ax_tenant.set_ylabel("Decision correctness (per tenant)")
    ax_tenant.set_ylim(0, 1.1)
    ax_tenant.set_title("Naive failure mode is tenant-asymmetric")
    ax_tenant.legend(loc="lower right", fontsize=8)
    ax_tenant.grid(axis="y", linestyle=":", alpha=0.4)

    fig.suptitle(
        f"Day 28 — Orchestrator policy engines on {data['n_scenarios']} scenarios "
        f"(cost projected at {data['cost_model']} prod-payload pricing)",
        fontsize=11,
    )
    plt.tight_layout()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(OUT_PATH, dpi=150, bbox_inches="tight")
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
