"""Render the Phase-2 architecture diagram for the Day 11 post.

Three boxes left-to-right (context-engine -> event bus -> orchestrator)
with the minimum labels needed to read the architecture. No metric
callouts, no caption strip. Matches the post's wiring-story insight.

Source of truth: reports/day11_phase2_report.md.
"""
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

OUT = Path(__file__).resolve().parent.parent / "results" / "samples" / "day11_phase2_architecture.png"

CE_COLOR = "#4A90E2"
BUS_COLOR = "#E67E22"
ORCH_COLOR = "#27AE60"
TEXT_DARK = "#2C3E50"
ARROW_COLOR = "#2C3E50"


def box(ax, x, y, w, h, title, subtitle, color):
    rect = FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0.05",
        linewidth=1.8, edgecolor=color, facecolor=color,
    )
    ax.add_patch(rect)
    ax.text(x + w / 2, y + h / 2 + 0.25, title,
            ha="center", va="center",
            fontsize=14, fontweight="bold", color="white")
    ax.text(x + w / 2, y + h / 2 - 0.30, subtitle,
            ha="center", va="center",
            fontsize=10, color="white")


def arrow(ax, x1, y1, x2, y2):
    a = FancyArrowPatch(
        (x1, y1), (x2, y2),
        arrowstyle="-|>", mutation_scale=20,
        linewidth=2.0, color=ARROW_COLOR,
    )
    ax.add_patch(a)


def main() -> None:
    fig, ax = plt.subplots(figsize=(12, 4), dpi=150)
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 4)
    ax.axis("off")

    ax.text(6, 3.55, "PennyCore — Phase 2 architecture",
            ha="center", fontsize=15, fontweight="bold", color=TEXT_DARK)

    box(ax, 0.4, 1.2, 3.2, 1.7, "context-engine", "FastAPI :8001", CE_COLOR)
    box(ax, 4.4, 1.2, 3.2, 1.7, "event bus", "Redis  |  in-memory", BUS_COLOR)
    box(ax, 8.4, 1.2, 3.2, 1.7, "orchestrator", "FastAPI :8002", ORCH_COLOR)

    arrow(ax, 3.6, 2.05, 4.4, 2.05)
    arrow(ax, 7.6, 2.05, 8.4, 2.05)

    ax.text(6, 0.55,
            "planner  →  policy  →  queue  →  audit",
            ha="center", fontsize=11, style="italic", color=TEXT_DARK)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(OUT, dpi=150, bbox_inches="tight", facecolor="white")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
