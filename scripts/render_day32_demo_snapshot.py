"""Render a static snapshot of the Day-32 demo scenario UI (Phase 6 wrap).

`streamlit run ui/demo_scenario_app.py` is the real article. This
script mocks the layout as a single matplotlib figure so the repo's
`results/samples/` carries the headline visual without depending on a
running Streamlit instance.

Output:
    results/samples/day32_phase6_demo_scenario.png
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.patches as patches
    import matplotlib.pyplot as plt

    from orchestrator.api import _get_pipeline_for_tests
    from ui.approver_data import get_action_detail
    from ui.demo_scenario import (
        JANE_TIMELINE,
        PHASE_FINDINGS,
        SCENARIO_TENANT,
        build_scenario_state,
    )

    _get_pipeline_for_tests().clear()
    state = build_scenario_state()

    fig = plt.figure(figsize=(17, 11), facecolor="white")
    fig.suptitle(
        "PennyCore — Jane's Mortgage Journey (Day 32, Phase 6 wrap)",
        fontsize=16,
        fontweight="bold",
        y=0.98,
    )

    # Timeline on the left.
    timeline_ax = fig.add_axes([0.03, 0.30, 0.50, 0.62])
    timeline_ax.text(
        0.0, 0.98, "🕐 Timeline (sidebar slider scrubs)",
        fontsize=12, fontweight="bold",
    )
    y = 0.92
    for i, step in enumerate(JANE_TIMELINE):
        timeline_ax.add_patch(
            patches.Rectangle(
                (0.0, y - 0.13), 1, 0.13,
                facecolor="white" if i != 4 else "#fff8e6",
                edgecolor="#c0c0c0" if i != 4 else "#dba000",
                linewidth=1.2,
            )
        )
        icon = "✅" if i < 2 else "⏳" if i == 2 else "⚪"
        timeline_ax.text(
            0.02, y - 0.03,
            f"{icon}  Step {i + 1}.  {step['title']}",
            fontsize=10, fontweight="bold",
        )
        timeline_ax.text(
            0.02, y - 0.07,
            step["description"][:120] + ("…" if len(step["description"]) > 120 else ""),
            fontsize=8, color="#444444", wrap=True,
        )
        if step.get("finding"):
            finding = PHASE_FINDINGS[step["finding"]]
            timeline_ax.text(
                0.02, y - 0.115,
                f"📎 Phase-{finding['phase']} finding: {finding['title']}",
                fontsize=8, color="#0c4da2", style="italic",
            )
        y -= 0.16
    timeline_ax.set_xlim(0, 1)
    timeline_ax.set_ylim(0, 1)
    timeline_ax.axis("off")

    # Action snapshot panel on the right.
    snap_ax = fig.add_axes([0.55, 0.30, 0.42, 0.62])
    snap_ax.text(
        0.0, 0.98, "🔍 System state — active step (Notify Loan Officer)",
        fontsize=12, fontweight="bold",
    )
    detail = get_action_detail(state["anomaly_action_id"], tenant_id=SCENARIO_TENANT)
    if detail is not None:
        snap_ax.text(0.0, 0.91, f"action_type     {detail['action_type']}", fontsize=9, family="monospace")
        snap_ax.text(0.0, 0.87, f"status          {detail['status']}", fontsize=9, family="monospace")
        snap_ax.text(0.0, 0.83, f"event_id        {detail['event_id']}", fontsize=9, family="monospace")
        snap_ax.text(0.0, 0.79, f"action_id       {detail['action_id'][:32]}…", fontsize=9, family="monospace")
        if detail.get("reasoning"):
            snap_ax.text(
                0.0, 0.73,
                f"reasoning  “{detail['reasoning']}”",
                fontsize=9, style="italic", color="#444444", wrap=True,
            )
        progress = detail["approval_progress"]
        if progress:
            required = max(progress["required_approvals"], 1)
            recorded = progress["approvals_recorded"]
            ratio = min(recorded / required, 1.0)
            snap_ax.add_patch(
                patches.Rectangle(
                    (0.0, 0.62), 0.6, 0.025,
                    facecolor="#e8e8e8", edgecolor="#a0a0a0",
                )
            )
            snap_ax.add_patch(
                patches.Rectangle(
                    (0.0, 0.62), 0.6 * ratio, 0.025, facecolor="#1f77b4"
                )
            )
            snap_ax.text(
                0.62, 0.625,
                f"{recorded}/{required} approvals "
                f"({progress['approvals_remaining']} remaining)",
                fontsize=9, family="monospace",
            )
            if progress.get("eligible_approvers"):
                snap_ax.text(
                    0.0, 0.58,
                    "Eligible: " + ", ".join(progress["eligible_approvers"]),
                    fontsize=8, color="#444444",
                )
        snap_ax.text(0.0, 0.52, "Audit trail", fontsize=10, fontweight="bold")
        ay = 0.47
        for entry in detail["audit_trail"]:
            snap_ax.text(
                0.0, ay,
                f"• {entry['kind']}   by {entry['actor_kind']}",
                fontsize=8, family="monospace",
            )
            ay -= 0.04
    snap_ax.set_xlim(0, 1)
    snap_ax.set_ylim(0, 1)
    snap_ax.axis("off")

    # Phase-findings strip along the bottom.
    findings_ax = fig.add_axes([0.03, 0.04, 0.94, 0.22])
    findings_ax.text(
        0.0, 1.0,
        "✨ Phase-3 / Phase-5 / Phase-6 findings referenced above",
        fontsize=12, fontweight="bold",
    )
    cards_per_row = 3
    findings_list = list(PHASE_FINDINGS.items())
    for idx, (_key, finding) in enumerate(findings_list):
        col = idx % cards_per_row
        row = idx // cards_per_row
        x = 0.0 + col * (1 / cards_per_row)
        y_top = 0.92 - row * 0.48
        findings_ax.add_patch(
            patches.Rectangle(
                (x + 0.005, y_top - 0.45),
                1 / cards_per_row - 0.01, 0.43,
                facecolor="#f7f9fb", edgecolor="#bcd0e6", linewidth=1.0,
            )
        )
        findings_ax.text(
            x + 0.02, y_top - 0.05,
            f"Phase {finding['phase']} — {finding['title']}",
            fontsize=9, fontweight="bold", wrap=True,
        )
        wrapped = finding["body"]
        if len(wrapped) > 280:
            wrapped = wrapped[:277] + "…"
        findings_ax.text(
            x + 0.02, y_top - 0.18,
            wrapped,
            fontsize=7.5, color="#333333", wrap=True,
        )
        findings_ax.text(
            x + 0.02, y_top - 0.42,
            finding["reference"],
            fontsize=7, family="monospace", color="#666666",
        )
    findings_ax.set_xlim(0, 1)
    findings_ax.set_ylim(-0.05, 1.0)
    findings_ax.axis("off")

    out_path = ROOT / "results" / "samples" / "day32_phase6_demo_scenario.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=130, facecolor="white")
    plt.close(fig)
    print(f"rendered {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
