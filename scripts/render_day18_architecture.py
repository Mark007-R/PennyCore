"""Render the comparison-study architecture diagram for the Day 18 post.

Layout:
  5 retrieval strategies  →  context-engine  →  harness  →  orchestrator  →  4 policy engines
                              (champ: hybrid)               (champ: declarative)

Three service boxes (left = retrieval, middle = shared harness, right = policy)
in the project's standard dark-theme accent colors. Strategy / engine names
listed as small channel-style boxes flanking the two outer services, with the
two champion strategies (hybrid, declarative) highlighted in the matching
service accent color.

No metric callouts overlaid; the only italic caption names the structural
count of the bake-off (9 strategies · 400 scenarios), not benchmark numbers.
Title format per Hard Rule 3: never "Phase N".

Source of truth: reports/day18_phase3_report.md, explainers/day18_explainer.md.
"""
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

OUT = Path(__file__).resolve().parent.parent / "results" / "samples" / "day18_phase3_architecture.png"

BG = "#1a1d2e"
CE_COLOR = "#4ea8de"        # context-engine blue
BUS_COLOR = "#ffb86b"       # harness / bus orange
ORCH_COLOR = "#52d273"      # orchestrator green
CHANNEL_EDGE = "#5a6275"
CHANNEL_FILL = "#262a3d"
ARROW_COLOR = "#cfd3dc"
ARROW_LIGHT = "#6e7588"
TEXT_LIGHT = "#e8eaed"
TEXT_DIM = "#9aa1ad"


def channel(ax, x, y, w, h, label, *, champion=False, accent=None):
    """Small box for a strategy/engine name. Champion uses accent fill+border."""
    if champion and accent is not None:
        edge = accent
        fill = accent
        text_color = "white"
        weight = "bold"
        lw = 1.6
    else:
        edge = CHANNEL_EDGE
        fill = CHANNEL_FILL
        text_color = TEXT_LIGHT
        weight = "normal"
        lw = 1.1
    rect = FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0.04",
        linewidth=lw, edgecolor=edge, facecolor=fill,
    )
    ax.add_patch(rect)
    ax.text(x + w / 2, y + h / 2, label,
            ha="center", va="center",
            fontsize=8.5, color=text_color, fontweight=weight)


def service(ax, x, y, w, h, title, subtitle, color):
    rect = FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0.05",
        linewidth=1.8, edgecolor=color, facecolor=color,
    )
    ax.add_patch(rect)
    ax.text(x + w / 2, y + h / 2 + 0.22, title,
            ha="center", va="center",
            fontsize=11.5, fontweight="bold", color="white")
    ax.text(x + w / 2, y + h / 2 - 0.30, subtitle,
            ha="center", va="center",
            fontsize=8.5, color="white")


def arrow(ax, x1, y1, x2, y2, color=ARROW_COLOR, width=1.7):
    a = FancyArrowPatch(
        (x1, y1), (x2, y2),
        arrowstyle="-|>", mutation_scale=15,
        linewidth=width, color=color,
    )
    ax.add_patch(a)


def main() -> None:
    fig, ax = plt.subplots(figsize=(14, 6.5), dpi=150)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    ax.set_xlim(0, 14)
    ax.set_ylim(0, 6.5)
    ax.axis("off")

    ax.text(7.0, 6.05, "PennyCore — comparison architecture",
            ha="center", fontsize=13, fontweight="bold", color=TEXT_LIGHT)

    # ---- left column: 5 retrieval strategies, hybrid is champion ----
    ch_w, ch_h = 1.25, 0.45
    ch_x = 0.30
    retrieval_labels = [
        ("naive_dump",  False),
        ("recency",     False),
        ("semantic",    False),
        ("summarized",  False),
        ("hybrid",      True),
    ]
    # vertical stack centered around y=3.0
    n = len(retrieval_labels)
    gap = 0.12
    stack_h = n * ch_h + (n - 1) * gap
    top_y = 3.0 + stack_h / 2 - ch_h
    retrieval_centers_y = []
    for i, (label, champ) in enumerate(retrieval_labels):
        cy = top_y - i * (ch_h + gap)
        retrieval_centers_y.append(cy + ch_h / 2)
        channel(ax, ch_x, cy, ch_w, ch_h, label,
                champion=champ, accent=CE_COLOR)

    # ---- three service boxes ----
    ce_x, ce_y, ce_w, ce_h = 2.30, 2.55, 2.70, 1.40
    bus_x, bus_y, bus_w, bus_h = 5.80, 2.55, 2.40, 1.40
    orch_x, orch_y, orch_w, orch_h = 9.00, 2.55, 2.70, 1.40

    service(ax, ce_x, ce_y, ce_w, ce_h, "context-engine", "champion: hybrid", CE_COLOR)
    service(ax, bus_x, bus_y, bus_w, bus_h, "harness", "shared fixtures", BUS_COLOR)
    service(ax, orch_x, orch_y, orch_w, orch_h, "orchestrator", "champion: declarative", ORCH_COLOR)

    ce_center_y = ce_y + ce_h / 2
    # arrows from each retrieval box to context-engine
    for cy in retrieval_centers_y:
        arrow(ax, ch_x + ch_w + 0.04, cy, ce_x - 0.04, ce_center_y,
              color=ARROW_LIGHT, width=1.2)

    # context-engine -> harness -> orchestrator
    arrow(ax, ce_x + ce_w + 0.04, ce_center_y, bus_x - 0.04, ce_center_y)
    arrow(ax, bus_x + bus_w + 0.04, ce_center_y, orch_x - 0.04, ce_center_y)

    # ---- right column: 4 policy engines, declarative is champion ----
    pol_w, pol_h = 1.55, 0.45
    pol_x = 12.05
    policy_labels = [
        ("naive_llm",     False),
        ("python_rules",  False),
        ("llm_judge",     False),
        ("declarative",   True),
    ]
    n2 = len(policy_labels)
    stack_h2 = n2 * pol_h + (n2 - 1) * gap
    top_y2 = 3.0 + stack_h2 / 2 - pol_h
    policy_centers_y = []
    for i, (label, champ) in enumerate(policy_labels):
        cy = top_y2 - i * (pol_h + gap)
        policy_centers_y.append(cy + pol_h / 2)
        channel(ax, pol_x, cy, pol_w, pol_h, label,
                champion=champ, accent=ORCH_COLOR)

    # arrows from orchestrator to each policy box
    for cy in policy_centers_y:
        arrow(ax, orch_x + orch_w + 0.04, ce_center_y, pol_x - 0.04, cy,
              color=ARROW_LIGHT, width=1.2)

    # ---- italic caption below the middle box: structural counts only ----
    ax.text(bus_x + bus_w / 2, bus_y - 0.45,
            "9 strategies  ·  400 scenarios",
            ha="center", fontsize=9, style="italic", color=TEXT_DIM)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(OUT, dpi=150, bbox_inches="tight", facecolor=BG)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
