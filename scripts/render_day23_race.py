"""Render the customer-linker race diagram for the Day 23 alt TWEET.

The tweet's spine is the GIL-hidden check-then-create (TOCTOU) race in the
customer linker: two concurrent callers both `find identity` -> no match,
both `create customer`, and the UNIQUE (tenant_id, identity) oracle lets one
win while the other raises IdentityCollision. The fix is catch-and-retry:
the loser re-walks the identity priority list and links to the winner.

Two-lane concurrency layout (a sequence, so more boxes than the single-flow
series images — but same dark theme / accent palette as Day 4 / 11 / 18):

  Thread A:  find -> (no match) -> create ─┐
                                            ├─ UNIQUE oracle ─> created ✓
  Thread B:  find -> (no match) -> create ─┘                 └> IdentityCollision
                                                                 -> catch + re-walk -> links to winner ✓

No metric callouts overlaid (Day 23 was mock mode; Hard Rule 3). The 10ms
latency + "7 of 8 callers" figures live in the tweet text, not on the image;
the image carries the mechanism only. Title per Hard Rule 3: never "Phase N".

Source of truth: context_engine/linking.py (resolve_customer catch-and-retry
on IdentityCollision), reports/day21_phase4_report.md (the race + 10ms-latency
reproducer), PROGRESS_LOG Day 21 entry.
"""
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle

OUT = Path(__file__).resolve().parent.parent / "results" / "samples" / "day23_phase4_race.png"

BG = "#1a1d2e"
BLUE = "#4ea8de"
ORANGE = "#ffb86b"
GREEN = "#52d273"
RED = "#e06c75"
CHANNEL_EDGE = "#5a6275"
CHANNEL_FILL = "#262a3d"
ARROW_COLOR = "#cfd3dc"
ARROW_LIGHT = "#6e7588"
TEXT_LIGHT = "#e8eaed"
TEXT_DIM = "#9aa1ad"


def box(ax, x, y, w, h, title, *, color=None, subtitle=None, text_color=None):
    """Rounded box. Filled accent box if `color` given, else neutral channel."""
    if color is not None:
        edge, fill, tc = color, color, (text_color or "white")
    else:
        edge, fill, tc = CHANNEL_EDGE, CHANNEL_FILL, (text_color or TEXT_LIGHT)
    rect = FancyBboxPatch(
        (x, y), w, h, boxstyle="round,pad=0.04",
        linewidth=1.6, edgecolor=edge, facecolor=fill,
    )
    ax.add_patch(rect)
    if subtitle:
        ax.text(x + w / 2, y + h / 2 + 0.16, title, ha="center", va="center",
                fontsize=9.5, fontweight="bold", color=tc)
        ax.text(x + w / 2, y + h / 2 - 0.18, subtitle, ha="center", va="center",
                fontsize=7.8, color=tc)
    else:
        ax.text(x + w / 2, y + h / 2, title, ha="center", va="center",
                fontsize=9.5, fontweight="bold", color=tc)


def arrow(ax, x1, y1, x2, y2, color=ARROW_COLOR, width=1.6):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle="-|>",
                                 mutation_scale=14, linewidth=width, color=color))


def main() -> None:
    fig, ax = plt.subplots(figsize=(13.2, 6.6), dpi=150)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    ax.set_xlim(0, 13.2)
    ax.set_ylim(0, 6.6)
    ax.axis("off")

    ax.text(6.6, 6.25, "PennyCore — the GIL-hidden linker race",
            ha="center", fontsize=13, fontweight="bold", color=TEXT_LIGHT)

    ay, by = 4.55, 2.05          # lane centres (Thread A / Thread B)
    bh = 0.92
    ay0, by0 = ay - bh / 2, by - bh / 2

    # shaded "window" band behind the find->create gap
    ax.add_patch(Rectangle((1.45, 1.15), 5.15, 4.30, facecolor="#ffffff",
                           alpha=0.04, edgecolor="none"))
    ax.text(4.02, 5.62, "10ms check→create window — both see no match",
            ha="center", fontsize=8.5, style="italic", color=TEXT_DIM)

    # lane labels
    ax.text(0.95, ay, "Thread A", ha="center", va="center",
            fontsize=8.5, color=TEXT_DIM, rotation=90)
    ax.text(0.95, by, "Thread B", ha="center", va="center",
            fontsize=8.5, color=TEXT_DIM, rotation=90)

    # column 1: find identity (no match)
    fx, fw = 1.65, 2.05
    box(ax, fx, ay0, fw, bh, "find identity", color=BLUE, subtitle="no match")
    box(ax, fx, by0, fw, bh, "find identity", color=BLUE, subtitle="no match")

    # column 2: create customer
    cx, cw = 4.45, 2.05
    box(ax, cx, ay0, cw, bh, "create", subtitle="customer")
    box(ax, cx, by0, cw, bh, "create", subtitle="customer")

    # UNIQUE oracle (centre, between the lanes)
    ox, oy, ow, oh = 7.30, 2.85, 2.00, 0.95
    box(ax, ox, oy, ow, oh, "UNIQUE", color=ORANGE, subtitle="(tenant, identity)")
    ocy = oy + oh / 2

    # column 3: outcomes
    rx, rw = 9.90, 2.45
    box(ax, rx, ay0, rw, bh, "created", color=GREEN, subtitle="winner ✓")
    box(ax, rx, by0, rw, bh, "IdentityCollision", color=RED, subtitle="race loser")

    # retry resolution (bottom span)
    tx, ty, tw, th = 7.30, 0.30, 5.05, 0.78
    box(ax, tx, ty, tw, th, "catch + re-walk", color=GREEN,
        subtitle="loser links to the winner's customer")

    # arrows: find -> create (each lane)
    arrow(ax, fx + fw + 0.04, ay, cx - 0.04, ay)
    arrow(ax, fx + fw + 0.04, by, cx - 0.04, by)
    # create -> oracle (both converge)
    arrow(ax, cx + cw + 0.04, ay, ox - 0.04, ocy, color=ARROW_LIGHT)
    arrow(ax, cx + cw + 0.04, by, ox - 0.04, ocy, color=ARROW_LIGHT)
    # oracle -> outcomes
    arrow(ax, ox + ow + 0.04, ocy, rx - 0.04, ay)
    arrow(ax, ox + ow + 0.04, ocy, rx - 0.04, by)
    # collision -> retry -> (back up to winner)
    arrow(ax, rx + rw / 2, by0 - 0.04, tx + tw - 0.6, ty + th + 0.04,
          color=RED, width=1.4)
    arrow(ax, tx + 0.6, ty + th + 0.04, rx + rw / 2, ay0 - 0.04,
          color=GREEN, width=1.4)

    # caption — structural, no numbers
    ax.text(6.6, -0.05, "check-then-create race  ·  catch-and-retry fix",
            ha="center", fontsize=9, style="italic", color=TEXT_DIM)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(OUT, dpi=150, bbox_inches="tight", facecolor=BG)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
