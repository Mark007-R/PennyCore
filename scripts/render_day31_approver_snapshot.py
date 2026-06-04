"""Render a static snapshot of the Day-31 approver dashboard (Phase 6).

`streamlit run ui/approver_app.py` is the real article. This script
produces a static mock of what the dashboard renders after `Seed demo
data` — a snapshot artifact under `results/samples/` so the repo
surfaces the deliverable without depending on a running Streamlit.

The mock is rendered from the LIVE data adapter — it pulls
`ui.approver_data.list_pending`, `recent_audit`, and
`get_action_detail` against a freshly-seeded pipeline. So if the
adapter changes, regenerating the artifact picks it up automatically.

Output:
    results/samples/day31_phase6_approver_dashboard.png
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
    from ui.approver_data import list_pending, recent_audit, seed_demo_data

    _get_pipeline_for_tests().clear()
    seed_demo_data(force=True)

    tenant = "tenant_acme_bank"
    pending = list_pending(tenant)
    audit = recent_audit(tenant, limit=8)

    fig = plt.figure(figsize=(16, 10), facecolor="white")
    fig.suptitle(
        f"PennyCore — Approver Dashboard (snapshot)   tenant: {tenant}",
        fontsize=15,
        fontweight="bold",
        y=0.98,
    )

    # Sidebar mock.
    sidebar_ax = fig.add_axes([0.02, 0.05, 0.18, 0.88])
    sidebar_ax.add_patch(
        patches.Rectangle(
            (0, 0), 1, 1, facecolor="#f0f2f6", edgecolor="#d0d3da"
        )
    )
    sidebar_ax.text(
        0.5, 0.95, "🧭 Controls", fontsize=12, fontweight="bold", ha="center"
    )
    sidebar_ax.text(0.05, 0.85, "Tenant", fontsize=9)
    sidebar_ax.add_patch(
        patches.Rectangle((0.05, 0.78), 0.9, 0.05, facecolor="white", edgecolor="#a0a0a0")
    )
    sidebar_ax.text(0.08, 0.795, tenant, fontsize=9, family="monospace")
    sidebar_ax.text(0.05, 0.70, "You (approver name)", fontsize=9)
    sidebar_ax.add_patch(
        patches.Rectangle((0.05, 0.63), 0.9, 0.05, facecolor="white", edgecolor="#a0a0a0")
    )
    sidebar_ax.text(0.08, 0.645, "compliance_alice", fontsize=9, family="monospace")
    sidebar_ax.text(0.05, 0.55, "Recent audit limit", fontsize=9)
    sidebar_ax.add_patch(
        patches.Rectangle((0.05, 0.50), 0.9, 0.02, facecolor="#1f77b4")
    )
    sidebar_ax.add_patch(
        patches.Rectangle((0.30, 0.485), 0.04, 0.05, facecolor="#1f77b4")
    )
    sidebar_ax.text(0.05, 0.46, "20", fontsize=9, family="monospace")
    sidebar_ax.add_patch(
        patches.Rectangle(
            (0.05, 0.25), 0.9, 0.06, facecolor="#ff4b4b", alpha=0.8
        )
    )
    sidebar_ax.text(
        0.5, 0.28, "🌱 Seed demo data",
        fontsize=10, fontweight="bold", color="white", ha="center"
    )
    sidebar_ax.set_xlim(0, 1)
    sidebar_ax.set_ylim(0, 1)
    sidebar_ax.axis("off")

    # Pending queue panel.
    queue_ax = fig.add_axes([0.22, 0.42, 0.50, 0.51])
    queue_ax.text(
        0.0, 0.97, "⏳ Pending approvals", fontsize=12, fontweight="bold"
    )
    y = 0.90
    for row in pending:
        queue_ax.add_patch(
            patches.Rectangle(
                (0.0, y - 0.20), 1, 0.20,
                facecolor="white", edgecolor="#c0c0c0", linewidth=1.2,
            )
        )
        queue_ax.text(
            0.02, y - 0.04,
            f"{row['action_type']}  for  {row['customer_id']}",
            fontsize=10, fontweight="bold",
        )
        queue_ax.text(
            0.02, y - 0.08,
            f"action {row['action_id'][:18]}…   event {row['event_id']}",
            fontsize=8, family="monospace", color="#666666",
        )
        queue_ax.text(
            0.02, y - 0.12,
            f"reasoning: {row['reasoning']}",
            fontsize=8, color="#444444",
        )
        progress = row.get("approval_progress")
        if progress is not None:
            required = max(progress["required_approvals"], 1)
            recorded = progress["approvals_recorded"]
            ratio = min(recorded / required, 1.0)
            queue_ax.add_patch(
                patches.Rectangle(
                    (0.02, y - 0.17), 0.5, 0.02,
                    facecolor="#e8e8e8", edgecolor="#a0a0a0",
                )
            )
            queue_ax.add_patch(
                patches.Rectangle(
                    (0.02, y - 0.17), 0.5 * ratio, 0.02, facecolor="#1f77b4"
                )
            )
            queue_ax.text(
                0.54, y - 0.16,
                f"{recorded}/{required} approvals "
                f"({progress['approvals_remaining']} remaining)",
                fontsize=8, family="monospace",
            )
            if progress["eligible_approvers"]:
                queue_ax.text(
                    0.02, y - 0.19,
                    "eligible: " + ", ".join(progress["eligible_approvers"]),
                    fontsize=7, color="#666666", style="italic",
                )
        queue_ax.add_patch(
            patches.Rectangle(
                (0.75, y - 0.18), 0.07, 0.03, facecolor="#ff4b4b"
            )
        )
        queue_ax.text(0.785, y - 0.165, "Approve", fontsize=7, color="white", ha="center")
        queue_ax.add_patch(
            patches.Rectangle(
                (0.83, y - 0.18), 0.07, 0.03, facecolor="#cccccc"
            )
        )
        queue_ax.text(0.865, y - 0.165, "Reject", fontsize=7, color="black", ha="center")
        queue_ax.add_patch(
            patches.Rectangle(
                (0.91, y - 0.18), 0.07, 0.03, facecolor="#888888"
            )
        )
        queue_ax.text(0.945, y - 0.165, "Details", fontsize=7, color="white", ha="center")
        y -= 0.24
    queue_ax.set_xlim(0, 1)
    queue_ax.set_ylim(0, 1)
    queue_ax.axis("off")

    # Detail panel.
    detail_ax = fig.add_axes([0.74, 0.42, 0.24, 0.51])
    detail_ax.text(
        0.0, 0.97, "🔍 Action detail", fontsize=12, fontweight="bold"
    )
    if pending:
        from ui.approver_data import get_action_detail

        detail = get_action_detail(
            pending[0]["action_id"], tenant_id=tenant
        )
        if detail is not None:
            detail_ax.text(0.0, 0.90, f"Action ID  {detail['action_id'][:18]}…", fontsize=8, family="monospace")
            detail_ax.text(0.0, 0.86, f"Status     {detail['status']}", fontsize=8, family="monospace")
            detail_ax.text(0.0, 0.82, f"Type       {detail['action_type']}", fontsize=8, family="monospace")
            detail_ax.text(0.0, 0.78, f"Event      {detail['event_id']}", fontsize=8, family="monospace")
            detail_ax.text(0.0, 0.74, f"Customer   {detail['customer_id']}", fontsize=8, family="monospace")
            if detail.get("reasoning"):
                detail_ax.text(
                    0.0, 0.68,
                    f"“{detail['reasoning']}”",
                    fontsize=9, style="italic", color="#444444",
                    wrap=True,
                )
            detail_ax.text(0.0, 0.58, "Audit trail", fontsize=10, fontweight="bold")
            ay = 0.53
            for entry in detail["audit_trail"]:
                detail_ax.text(
                    0.0, ay,
                    f"• {entry['kind']}  by {entry['actor_kind']}",
                    fontsize=8, family="monospace",
                )
                ay -= 0.04
    detail_ax.set_xlim(0, 1)
    detail_ax.set_ylim(0, 1)
    detail_ax.axis("off")

    # Recent audit log.
    audit_ax = fig.add_axes([0.22, 0.05, 0.76, 0.32])
    audit_ax.text(
        0.0, 0.96, "📜 Recent audit log", fontsize=12, fontweight="bold"
    )
    header_y = 0.88
    audit_ax.add_patch(
        patches.Rectangle((0, header_y), 1, 0.06, facecolor="#e8e8e8")
    )
    for x, label in zip(
        [0.01, 0.10, 0.30, 0.50, 0.70, 0.85],
        ["id", "kind", "tenant", "action_id", "event_id", "actor"],
    ):
        audit_ax.text(x, header_y + 0.02, label, fontsize=8, fontweight="bold")

    ay = header_y - 0.08
    for entry in audit:
        audit_ax.text(0.01, ay, str(entry["id"]), fontsize=8, family="monospace")
        audit_ax.text(0.10, ay, entry["kind"], fontsize=8, family="monospace")
        audit_ax.text(0.30, ay, entry["tenant_id"][:18], fontsize=8, family="monospace")
        audit_ax.text(0.50, ay, (entry["action_id"] or "")[:18], fontsize=7, family="monospace", color="#666")
        audit_ax.text(0.70, ay, entry["event_id"] or "", fontsize=8, family="monospace")
        audit_ax.text(0.85, ay, f"{entry['actor_kind']}({entry['actor_id'] or '-'})", fontsize=7, family="monospace")
        ay -= 0.10
    audit_ax.set_xlim(0, 1)
    audit_ax.set_ylim(0, 1)
    audit_ax.axis("off")

    out_path = ROOT / "results" / "samples" / "day31_phase6_approver_dashboard.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=130, facecolor="white")
    plt.close(fig)
    print(f"rendered {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
