"""Render the prompt-injection three-layer diagram for the Day 23 TWEET.

The tweet's spine is the three-layer mitigation:
  per-tenant quarantine flag  ->  regex sanitiser  ->  audit-stamp on proposal

Layout (matches the Day 4 / 11 / 18 dark-theme attachments):

  6 OWASP LLM-01 families  →  front door      →  sanitiser        →  audit log
                              (flag + ingest)     (strip + wrap)      (stamp flags)

This is the image that pairs with the X/Twitter post (the LinkedIn post keeps
the broader 5-pillar `day23_phase4_architecture.png`). The six attack families
are listed as small channel-style boxes feeding the flow.

No metric callouts overlaid (Day 23 was mock mode; benchmark numbers are out
per Hard Rule 3). Family labels are the exact `InjectionFlag` `.value` strings
(what gets serialised into payload["_injection_flags"] and the audit log),
verbatim from context_engine/safety/prompt_injection.py. The attack-corpus
SIZE is intentionally left off — the diary docs say "32" but the live code
(tests/adversarial/test_prompt_injection.py, KNOWN_ATTACKS_MUST_FLAG) holds 33.
Title format per Hard Rule 3: never "Phase N".

Source of truth: context_engine/safety/prompt_injection.py (InjectionFlag
enum, BEGIN_UNTRUSTED/END_UNTRUSTED markers), context_engine/quarantine.py
(per-tenant ring), orchestrator/planner.py (_injection_flags stamp).
"""
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

OUT = Path(__file__).resolve().parent.parent / "results" / "samples" / "day23_phase4_injection.png"

BG = "#1a1d2e"
CE_COLOR = "#4ea8de"        # front door — blue (context-engine accent)
BUS_COLOR = "#ffb86b"       # sanitiser — orange (harness/bus accent)
ORCH_COLOR = "#52d273"      # audit log — green (orchestrator accent)
CHANNEL_EDGE = "#5a6275"
CHANNEL_FILL = "#262a3d"
ARROW_COLOR = "#cfd3dc"
ARROW_LIGHT = "#6e7588"
TEXT_LIGHT = "#e8eaed"
TEXT_DIM = "#9aa1ad"


def channel(ax, x, y, w, h, label):
    """Small box for an attack-family name (neutral channel style)."""
    rect = FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0.04",
        linewidth=1.1, edgecolor=CHANNEL_EDGE, facecolor=CHANNEL_FILL,
    )
    ax.add_patch(rect)
    ax.text(x + w / 2, y + h / 2, label,
            ha="center", va="center",
            fontsize=6.8, color=TEXT_LIGHT)


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
    fig, ax = plt.subplots(figsize=(13, 6.3), dpi=150)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    ax.set_xlim(0, 11.7)
    ax.set_ylim(0, 6.3)
    ax.axis("off")

    ax.text(5.85, 5.95, "PennyCore — prompt-injection defence",
            ha="center", fontsize=13, fontweight="bold", color=TEXT_LIGHT)

    # ---- left column: the 6 OWASP LLM-01 families ----
    # Labels are the exact InjectionFlag .value strings (what gets
    # serialised into payload["_injection_flags"] and the audit log),
    # verbatim from context_engine/safety/prompt_injection.py.
    ch_w, ch_h = 2.45, 0.46
    ch_x = 0.15
    family_labels = [
        "instruction_override",
        "role_impersonation",
        "system_prompt_leak_attempt",
        "jailbreak_persona",
        "delimiter_abuse",
        "policy_override",
    ]
    n = len(family_labels)
    gap = 0.12
    stack_h = n * ch_h + (n - 1) * gap
    top_y = 3.0 + stack_h / 2 - ch_h
    family_centers_y = []
    for i, label in enumerate(family_labels):
        cy = top_y - i * (ch_h + gap)
        family_centers_y.append(cy + ch_h / 2)
        channel(ax, ch_x, cy, ch_w, ch_h, label)

    # ---- three service boxes: the three-layer pattern ----
    fd_x, fd_y, fd_w, fd_h = 2.90, 2.55, 2.25, 1.40
    san_x, san_y, san_w, san_h = 5.85, 2.55, 2.25, 1.40
    aud_x, aud_y, aud_w, aud_h = 8.80, 2.55, 2.25, 1.40

    service(ax, fd_x, fd_y, fd_w, fd_h, "front door", "flag + ingest", CE_COLOR)
    service(ax, san_x, san_y, san_w, san_h, "sanitiser", "strip + wrap", BUS_COLOR)
    service(ax, aud_x, aud_y, aud_w, aud_h, "audit log", "stamp flags", ORCH_COLOR)

    center_y = fd_y + fd_h / 2
    # arrows from each family box to the front door
    for cy in family_centers_y:
        arrow(ax, ch_x + ch_w + 0.04, cy, fd_x - 0.04, center_y,
              color=ARROW_LIGHT, width=1.2)

    # front door -> sanitiser -> audit log
    arrow(ax, fd_x + fd_w + 0.04, center_y, san_x - 0.04, center_y)
    arrow(ax, san_x + san_w + 0.04, center_y, aud_x - 0.04, center_y)

    # ---- small descriptive sub-labels under each layer ----
    ax.text(fd_x + fd_w / 2, fd_y - 0.40,
            "per-tenant quarantine ring",
            ha="center", fontsize=8, style="italic", color=TEXT_DIM)
    ax.text(san_x + san_w / 2, san_y - 0.40,
            "BEGIN_UNTRUSTED … END_UNTRUSTED",
            ha="center", fontsize=8, style="italic", color=TEXT_DIM)
    ax.text(aud_x + aud_w / 2, aud_y - 0.40,
            "_injection_flags on proposal",
            ha="center", fontsize=8, style="italic", color=TEXT_DIM)

    # ---- italic caption above the flow: verified structural count only ----
    # "6 families" = exactly 6 InjectionFlag enum members (verified).
    # Attack-corpus size intentionally omitted: diary says 32, code has 33.
    ax.text(san_x + san_w / 2, san_y + san_h + 0.45,
            "6 OWASP LLM-01 families  ·  regex sanitiser",
            ha="center", fontsize=9, style="italic", color=TEXT_DIM)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(OUT, dpi=150, bbox_inches="tight", facecolor=BG)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
