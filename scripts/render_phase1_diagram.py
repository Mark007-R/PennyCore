"""Render the Phase 1 wrap-up architecture diagram for the Day 4 social post.

STRICT RULES for this diagram:
1. Only show what exists at the end of Phase 1 (Day 4, 2026-05-07).
   No forward references to Phase 2+ behavior.
2. Use plain English a non-technical reader can follow.
   Technical names (context-engine, orchestrator, Postgres, Redis) appear
   only as small subtitles under the plain-English label.

What exists in Phase 1:
- 4 containers running as scaffolds (postgres, redis, context-engine, orchestrator)
- The two web services boot and answer "I'm alive" / "I'm ready" health pings
- The database has 12 tables auto-loaded on first start
- Three rules the database itself protects:
    - the same event cannot be processed twice
    - customers from different companies never see each other's data
    - removing a customer also removes their messages (no orphan rows)
- A 2-second quality check runs every commit (45 automated checks pass)
- Healthcheck-gated startup: services wait for db + bus before they start

Output: results/samples/day04_phase1_architecture.png
"""
from __future__ import annotations

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

# ---- palette ---------------------------------------------------------------
BG = "#0F1117"
CARD = "#1B1F2A"
ACCENT = "#5EC2FF"     # memory service
ACCENT_2 = "#FFB454"   # decision service
DATA = "#A2E4B8"       # database
BUS = "#E07AC9"        # message bus
TEXT = "#E6EAF2"
MUTED = "#8A93A6"
RULE = "#3A4053"

# ---- figure ----------------------------------------------------------------
fig, ax = plt.subplots(figsize=(13, 8.6), dpi=200)
fig.patch.set_facecolor(BG)
ax.set_facecolor(BG)
ax.set_xlim(0, 13)
ax.set_ylim(0, 8.6)
ax.set_axis_off()


def card(x, y, w, h, edge_color, fill=CARD, lw=1.8):
    box = FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0.02,rounding_size=0.20",
        linewidth=lw, edgecolor=edge_color, facecolor=fill,
    )
    ax.add_patch(box)


def arrow(x1, y1, x2, y2, color=MUTED, ls="-", lw=1.4):
    a = FancyArrowPatch(
        (x1, y1), (x2, y2),
        arrowstyle="-|>", mutation_scale=12,
        color=color, linewidth=lw, linestyle=ls,
    )
    ax.add_patch(a)


# ---- title -----------------------------------------------------------------
ax.text(
    6.5, 8.20, "PennyCore — Foundation Phase Complete",
    color=TEXT, fontsize=21, fontweight="bold",
    ha="center", va="center", family="DejaVu Sans",
)
ax.text(
    6.5, 7.78,
    "Four pieces wired together. One command starts everything. The real work begins next.",
    color=MUTED, fontsize=12, ha="center", va="center", family="DejaVu Sans",
)

# ---- service cards (top row) — plain-English labels -----------------------
# Memory service
card(0.6, 4.85, 5.5, 2.35, ACCENT)
ax.text(0.90, 6.85, "Memory Service", color=ACCENT, fontsize=15, fontweight="bold")
ax.text(0.90, 6.55, "(internal name: context-engine)", color=MUTED, fontsize=8.5, style="italic")
ax.text(0.90, 6.10, "Will remember every customer conversation", color=TEXT, fontsize=10.5)
ax.text(0.90, 5.82, "across email, chat, SMS, and voice.", color=TEXT, fontsize=10.5)
ax.text(0.90, 5.35, 'Today: an empty shell that just says "I\'m alive".', color=MUTED, fontsize=9, style="italic")

# Decision service
card(6.9, 4.85, 5.5, 2.35, ACCENT_2)
ax.text(7.20, 6.85, "Decision Service", color=ACCENT_2, fontsize=15, fontweight="bold")
ax.text(7.20, 6.55, "(internal name: orchestrator)", color=MUTED, fontsize=8.5, style="italic")
ax.text(7.20, 6.10, "Will decide what action to take next", color=TEXT, fontsize=10.5)
ax.text(7.20, 5.82, "and route risky ones to a human.", color=TEXT, fontsize=10.5)
ax.text(7.20, 5.35, 'Today: an empty shell that just says "I\'m alive".', color=MUTED, fontsize=9, style="italic")

# ---- data layer (bottom row) ----------------------------------------------
# Database
card(0.6, 1.85, 5.5, 2.35, DATA)
ax.text(0.90, 3.85, "Database", color=DATA, fontsize=15, fontweight="bold")
ax.text(0.90, 3.55, "(internal name: Postgres)", color=MUTED, fontsize=8.5, style="italic")
ax.text(0.90, 3.15, "12 tables ready, loaded on first start.", color=TEXT, fontsize=10.5)
ax.text(0.90, 2.78, "Two rules the database itself protects:", color=TEXT, fontsize=9.5)
ax.text(0.90, 2.48, "  · The same event can't be processed twice.", color=TEXT, fontsize=9)
ax.text(0.90, 2.20, "  · Customers from different companies", color=TEXT, fontsize=9)
ax.text(0.90, 2.00, "    never see each other's data.", color=TEXT, fontsize=9)

# Message bus
card(6.9, 1.85, 5.5, 2.35, BUS)
ax.text(7.20, 3.85, "Message Bus", color=BUS, fontsize=15, fontweight="bold")
ax.text(7.20, 3.55, "(internal name: Redis)", color=MUTED, fontsize=8.5, style="italic")
ax.text(7.20, 3.15, "Will let the two services talk in real time.", color=TEXT, fontsize=10.5)
ax.text(7.20, 2.55, "Today: running and healthy.", color=MUTED, fontsize=9, style="italic")
ax.text(7.20, 2.27, "No traffic yet.", color=MUTED, fontsize=9, style="italic")

# ---- arrows: the only real wiring in Phase 1 (startup ordering) -----------
# Short, clean dotted arrows from each db-layer card up to the service above it.
# Each service waits on BOTH database and bus; we draw one arrow per service for clarity.
arrow(2.4, 4.22, 2.4, 4.80, color=MUTED, ls=":", lw=1.1)   # memory ← database
arrow(8.7, 4.22, 8.7, 4.80, color=MUTED, ls=":", lw=1.1)   # decision ← message bus
arrow(4.6, 4.22, 4.6, 4.80, color=MUTED, ls=":", lw=1.1)   # memory ← (also bus, drawn from db side for layout)
arrow(10.4, 4.22, 10.4, 4.80, color=MUTED, ls=":", lw=1.1) # decision ← (also db)

# Plain-English label for the arrows — placed in a dedicated band with a
# subtle background card so it stays readable above the arrows.
card(2.5, 4.36, 8.0, 0.42, RULE, fill=BG, lw=0.0)
ax.text(
    6.5, 4.57,
    "Both services wait for the database and message bus to be healthy before they start.",
    color=MUTED, fontsize=10, ha="center", va="center", style="italic",
)

# ---- footer rule + Phase 1 stats in plain English -------------------------
ax.plot([0.6, 12.4], [1.42, 1.42], color=RULE, lw=1.0)

# Plain-English footer. Each value is from Phase 1 only.
#   12 → tables (report line 342)
#   45 → tests (report line 345)
#   91% → coverage on shipped code (report line 349)
#   2 sec → local CI runtime (report line 252)
#   4 → docker services (report line 351)
stats = [
    ("12",     "tables ready"),
    ("45",     "tests pass"),
    ("91%",    "code tested"),
    ("~2 sec", "full check"),
    ("4",      "pieces · 1 command"),
]
n = len(stats)
slot = 11.6 / n
for i, (val, label) in enumerate(stats):
    cx = 0.6 + slot * (i + 0.5)
    ax.text(cx, 0.92, val, color=TEXT, fontsize=16, fontweight="bold", ha="center", va="center")
    ax.text(cx, 0.55, label, color=MUTED, fontsize=9, ha="center", va="center")

# ---- save -----------------------------------------------------------------
out = "results/samples/day04_phase1_architecture.png"
plt.savefig(out, facecolor=BG, bbox_inches="tight", pad_inches=0.30)
print(f"wrote {out}")
