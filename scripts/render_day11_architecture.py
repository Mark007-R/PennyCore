"""Render the MVP architecture diagram for the Day 11 post.

Layout:
  chat / email / SMS  →  context-engine  →  event bus  →  orchestrator
                                                          (planner → policy → queue → audit)

The 3 input channels make the post's "5 events, 3 channels, 1 customer_id"
claim visual. The pipeline line is anchored under the orchestrator box.
No metric callouts. Title format per the user policy override: never "Phase N".

Source of truth: reports/day11_phase2_report.md.
"""
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

OUT = Path(__file__).resolve().parent.parent / "results" / "samples" / "day11_phase2_architecture.png"

BG = "#1a1d2e"
CE_COLOR = "#4ea8de"
BUS_COLOR = "#ffb86b"
ORCH_COLOR = "#52d273"
CHANNEL_EDGE = "#5a6275"
CHANNEL_FILL = "#262a3d"
ARROW_COLOR = "#cfd3dc"
ARROW_LIGHT = "#6e7588"
TEXT_LIGHT = "#e8eaed"
TEXT_DIM = "#9aa1ad"


def channel(ax, x, y, w, h, label):
    rect = FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0.04",
        linewidth=1.1, edgecolor=CHANNEL_EDGE, facecolor=CHANNEL_FILL,
    )
    ax.add_patch(rect)
    ax.text(x + w / 2, y + h / 2, label,
            ha="center", va="center",
            fontsize=8.5, color=TEXT_LIGHT)


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
    fig, ax = plt.subplots(figsize=(13, 6), dpi=150)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    ax.set_xlim(0, 13)
    ax.set_ylim(0, 6)
    ax.axis("off")

    ax.text(6.5, 5.55, "PennyCore — MVP architecture",
            ha="center", fontsize=13, fontweight="bold", color=TEXT_LIGHT)

    ch_w, ch_h = 1.30, 0.55
    ch_x = 0.30
    ch_centers = {"chat": 4.25, "email": 3.45, "sms": 2.65}
    for label, cy in ch_centers.items():
        channel(ax, ch_x, cy - ch_h / 2, ch_w, ch_h, label)

    ce_x, ce_y, ce_w, ce_h = 3.40, 2.70, 3.00, 1.50
    bus_x, bus_y, bus_w, bus_h = 7.10, 2.70, 2.60, 1.50
    orch_x, orch_y, orch_w, orch_h = 10.30, 2.70, 2.50, 1.50

    service(ax, ce_x, ce_y, ce_w, ce_h, "context-engine", "FastAPI :8001", CE_COLOR)
    service(ax, bus_x, bus_y, bus_w, bus_h, "event bus", "Redis or in-memory", BUS_COLOR)
    service(ax, orch_x, orch_y, orch_w, orch_h, "orchestrator", "FastAPI :8002", ORCH_COLOR)

    ce_center_y = ce_y + ce_h / 2
    for cy in ch_centers.values():
        arrow(ax, ch_x + ch_w + 0.04, cy, ce_x - 0.04, ce_center_y,
              color=ARROW_LIGHT, width=1.3)

    arrow(ax, ce_x + ce_w + 0.04, ce_center_y, bus_x - 0.04, ce_center_y)
    arrow(ax, bus_x + bus_w + 0.04, ce_center_y, orch_x - 0.04, ce_center_y)

    ax.text((ce_x + ce_w + bus_x) / 2, ce_y + ce_h + 0.25,
            "1 customer_id",
            ha="center", fontsize=8, style="italic", color=TEXT_DIM)

    ax.text(orch_x + orch_w / 2, orch_y - 0.50,
            "planner  →  policy  →  queue  →  audit",
            ha="center", fontsize=8.5, style="italic", color=TEXT_LIGHT)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(OUT, dpi=150, bbox_inches="tight", facecolor=BG)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
