"""Render assets/architecture.png.

Pillow, drawn at 2x and downsampled. Dark card with light text so it reads on
both the GitHub light and dark themes.

Run:  python assets/make_architecture.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

S = 2
W, H = 980 * S, 640 * S
OUT = Path(__file__).with_name("architecture.png")

BG, FG, MUTED, LINE = (13, 17, 23), (201, 209, 217), (139, 148, 158), (110, 118, 129)
ACCENT, GREEN, AMBER = (188, 140, 255), (63, 185, 80), (210, 153, 34)
FONTS = r"C:\Windows\Fonts"


def font(n, s):
    return ImageFont.truetype(f"{FONTS}\\{n}", s * S)


f_title, f_head = font("seguisb.ttf", 15), font("seguisb.ttf", 12)
f_small, f_lbl = font("segoeui.ttf", 10), font("segoeuii.ttf", 9)

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)


def box(x, y, w, h, c=LINE, width=2):
    d.rounded_rectangle([x * S, y * S, (x + w) * S, (y + h) * S],
                        radius=6 * S, outline=c, width=int(width * S))


def text(x, y, s, f=f_small, fill=MUTED, anchor="mm"):
    d.text((x * S, y * S), s, font=f, fill=fill, anchor=anchor)


def _head(p0, p1, c, size=6):
    (x0, y0), (x1, y1) = p0, p1
    dx, dy = x1 - x0, y1 - y0
    dist = max((dx * dx + dy * dy) ** .5, 1e-6)
    ux, uy = dx / dist, dy / dist
    px, py = -uy, ux
    s = size * S
    d.polygon([(x1, y1),
               (x1 - ux * s + px * s * .5, y1 - uy * s + py * s * .5),
               (x1 - ux * s - px * s * .5, y1 - uy * s - py * s * .5)], fill=c)


def arrow(pts, c=LINE, w=1.5):
    pts = [(x * S, y * S) for x, y in pts]
    for i in range(len(pts) - 1):
        d.line([pts[i], pts[i + 1]], fill=c, width=int(w * S))
    _head(pts[-2], pts[-1], c)


text(490, 26, "AI-Customer-Ops-Engine — a memory layer and a decision layer, sharing one schema and one audit log",
     f_title, FG)

# ── channels ────────────────────────────────────────────────────────────────
box(30, 54, 380, 50)
text(220, 70, "Events — chat · email · SMS · voice", f_head, FG)
text(220, 89, "one customer, many channels, over days or weeks")

# ── context engine ──────────────────────────────────────────────────────────
arrow([(220, 104), (220, 130)])
box(30, 132, 380, 190, ACCENT)
text(52, 152, "context_engine — the memory layer", f_head, FG, anchor="lm")
rows = [
    ("Ingest + identity linking", "one customer across channels"),
    ("Retrieval — 5 strategies", "recency · semantic · summarized · hybrid · rerank"),
    ("Token-budgeted brief", "8K budget, bounded by construction"),
    ("Prompt-injection defence", "strips customer-controlled fields"),
    ("Semantic cache", "reuse near-identical briefs"),
]
for i, (t, s) in enumerate(rows):
    y = 172 + i * 30
    text(52, y, t, f_small, FG, anchor="lm")
    text(52, y + 13, s, f_lbl, MUTED, anchor="lm")

# ── orchestrator ────────────────────────────────────────────────────────────
arrow([(410, 227), (566, 227)])
text(488, 214, "brief", f_lbl, MUTED)
box(570, 132, 380, 190, ACCENT)
text(592, 152, "orchestrator — the decision layer", f_head, FG, anchor="lm")
rows2 = [
    ("Planner asks the LLM what to do", "proposes an action"),
    ("Policy engine gates it", "declarative YAML — 100% correct, $0"),
    ("Approval queue", "N-of-M quorum for high-risk actions"),
    ("Executor", "runs only what policy allowed"),
    ("Audit", "one row per proposal, decision, execution"),
]
for i, (t, s) in enumerate(rows2):
    y = 172 + i * 30
    text(592, y, t, f_small, FG, anchor="lm")
    text(592, y + 13, s, f_lbl, MUTED, anchor="lm")

# ── shared store ────────────────────────────────────────────────────────────
arrow([(220, 322), (220, 352)])
arrow([(760, 322), (760, 352)])
box(30, 354, 920, 62, GREEN)
text(490, 376, "contracts/ — shared Pydantic models · Postgres + Redis · one audit_log", f_head, FG)
text(490, 395, "every query scoped by tenant_id · cross-tenant reads 404 · replays are no-ops")

# ── the finding ─────────────────────────────────────────────────────────────
box(30, 438, 920, 90, AMBER)
text(490, 460, "What the policy comparison found", f_head, FG)
text(490, 481, "Declarative YAML and LLM-as-judge tie on correctness (1.00). The declarative table costs $0 and is 11x faster.")
text(490, 500, "But the AI-startup default — paste the policy into the prompt — scores 0.54 and gets every reject scenario wrong,")
text(490, 519, "silently routing high-risk actions to auto-execute. That is the failure mode regulators audit for.")

text(30, 556, "All benchmark numbers were produced in mock_proxy mode — no live LLM call succeeded during the runs",
     f_lbl, AMBER, anchor="lm")
text(30, 576, "Quality is a deterministic token-recall proxy, not an LLM-as-judge — see the caveat in the README",
     f_lbl, AMBER, anchor="lm")
text(30, 600, "871 tests · 90% coverage on core packages · takehome scorecard 5/5 and 6/6",
     f_lbl, MUTED, anchor="lm")

img.resize((W // S, H // S), Image.LANCZOS).save(OUT, "PNG", optimize=True)
print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB)")
