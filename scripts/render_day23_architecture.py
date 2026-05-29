"""Render the hardening architecture diagram for the Day 23 (Phase 4 wrap) post.

Layout — the whole-phase outcome as a simple fan flow (matches the Day 4 /
11 / 18 dark-theme attachments for series consistency):

      MVP  →  [ idempotency / tenant isolation / race conditions /        →  hardened
   (memory +    load test / failure modes ]                                 (production-
    decisions)   five hardening passes applied to the shipped system          ready)

Left anchor = the MVP + champions that entered the phase; the five middle
boxes are the five hardening passes (one per day, in order); right anchor =
the production-hardened system that exited. Fan-in / fan-out arrows mirror
the Day-18 template.

No benchmark / metric callouts overlaid (Day 23 was mock mode; Hard Rule 3
forbids them). The only italic caption is a structural count verified
against source: 5 hardening passes (Days 19-23) and the 653-test green
baseline (636 main commit + 17 backfill, per PROGRESS_LOG / reports).
Title format per Hard Rule 3: never "Phase N".

Source of truth for the five pillar names: PROGRESS_LOG.md Day 19-23 entry
titles (idempotency, multi-tenant isolation, race-condition, load test,
failure modes). 653 tests: reports/day23_phase4_report.md backfill addendum.
"""
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

OUT = Path(__file__).resolve().parent.parent / "results" / "samples" / "day23_phase4_architecture.png"

BG = "#1a1d2e"
CE_COLOR = "#4ea8de"        # MVP anchor — blue (context-engine accent)
BUS_COLOR = "#ffb86b"       # (reserved) harness/bus accent
ORCH_COLOR = "#52d273"      # hardened anchor — green (orchestrator accent)
CHANNEL_EDGE = "#5a6275"
CHANNEL_FILL = "#262a3d"
ARROW_COLOR = "#cfd3dc"
ARROW_LIGHT = "#6e7588"
TEXT_LIGHT = "#e8eaed"
TEXT_DIM = "#9aa1ad"


def pillar(ax, x, y, w, h, label):
    """Small box for one hardening pass (neutral channel style)."""
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
    fig, ax = plt.subplots(figsize=(13, 6.5), dpi=150)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    ax.set_xlim(0, 11.6)
    ax.set_ylim(0, 6.5)
    ax.axis("off")

    ax.text(5.8, 6.10, "PennyCore — hardening architecture",
            ha="center", fontsize=13, fontweight="bold", color=TEXT_LIGHT)

    # ---- left anchor: the MVP + champions that entered the phase ----
    mvp_x, mvp_y, mvp_w, mvp_h = 0.55, 2.55, 2.35, 1.40
    service(ax, mvp_x, mvp_y, mvp_w, mvp_h, "MVP", "memory + decisions", CE_COLOR)
    mvp_cy = mvp_y + mvp_h / 2

    # ---- middle: the five hardening passes (one per day, in order) ----
    pl_w, pl_h = 2.45, 0.55
    pl_x = 4.05
    pillar_labels = [
        "idempotency",
        "tenant isolation",
        "race conditions",
        "load test",
        "failure modes",
    ]
    n = len(pillar_labels)
    gap = 0.18
    stack_h = n * pl_h + (n - 1) * gap
    top_y = 3.25 + stack_h / 2 - pl_h
    pillar_centers_y = []
    for i, label in enumerate(pillar_labels):
        cy = top_y - i * (pl_h + gap)
        pillar_centers_y.append(cy + pl_h / 2)
        pillar(ax, pl_x, cy, pl_w, pl_h, label)

    # ---- right anchor: the production-hardened system that exited ----
    hd_x, hd_y, hd_w, hd_h = 8.70, 2.55, 2.35, 1.40
    service(ax, hd_x, hd_y, hd_w, hd_h, "hardened", "production-ready", ORCH_COLOR)
    hd_cy = hd_y + hd_h / 2

    # ---- fan-in: MVP -> each pillar ; fan-out: each pillar -> hardened ----
    for cy in pillar_centers_y:
        arrow(ax, mvp_x + mvp_w + 0.04, mvp_cy, pl_x - 0.04, cy,
              color=ARROW_LIGHT, width=1.2)
        arrow(ax, pl_x + pl_w + 0.04, cy, hd_x - 0.04, hd_cy,
              color=ARROW_LIGHT, width=1.2)

    # ---- italic caption below the middle stack: verified structural counts ----
    bottom_y = top_y - (n - 1) * (pl_h + gap)
    ax.text(pl_x + pl_w / 2, bottom_y - 0.45,
            "5 hardening passes  ·  653 tests green",
            ha="center", fontsize=9, style="italic", color=TEXT_DIM)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(OUT, dpi=150, bbox_inches="tight", facecolor=BG)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
