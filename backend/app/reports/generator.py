"""Run reports: Markdown, JSON and PDF.

The report is the user-facing proof that the agent did what it said. It is built
from persisted rows only — never from in-memory state — so the report you
download is reproducible from the database long after the process is gone.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.logging import get_logger, log_event
from app.core.models import (
    Approval,
    ApprovalStatus,
    Run,
    RunEvent,
    RunStatus,
    Screenshot,
    Step,
    StepStatus,
)

logger = get_logger(__name__)

STATUS_ICON = {
    StepStatus.DONE.value: "OK",
    StepStatus.FAILED.value: "FAIL",
    StepStatus.SKIPPED.value: "SKIP",
    StepStatus.AWAITING_APPROVAL.value: "WAIT",
    StepStatus.RUNNING.value: "....",
    StepStatus.PENDING.value: "....",
}
DECISION_LABEL = {
    ApprovalStatus.APPROVED.value: "approved",
    ApprovalStatus.EDITED.value: "approved with edits",
    ApprovalStatus.REJECTED.value: "rejected",
    ApprovalStatus.TIMED_OUT.value: "timed out (auto-rejected)",
    ApprovalStatus.CANCELLED.value: "cancelled",
}


def _fmt_ms(value: float | None) -> str:
    if not value:
        return "-"
    return f"{value / 1000:.1f}s" if value >= 1000 else f"{value:.0f}ms"


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m {int(seconds % 60)}s"


def _cell(text: str, limit: int = 70) -> str:
    flat = (text or "").replace("|", "\\|").replace("\n", " ")
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _status_label(status: str) -> str:
    return {
        RunStatus.COMPLETED.value: "Completed",
        RunStatus.FAILED.value: "Failed",
        RunStatus.CANCELLED.value: "Cancelled",
        RunStatus.RUNNING.value: "Running",
        RunStatus.AWAITING_APPROVAL.value: "Waiting for your approval",
        RunStatus.AWAITING_PLAN_APPROVAL.value: "Waiting for plan review",
        RunStatus.PLANNING.value: "Planning",
    }.get(status, status)


def _public_args(step: Step) -> dict[str, Any]:
    return {k: v for k, v in (step.args or {}).items() if not k.startswith("__")}


def _event_tail(events: list[RunEvent], limit: int = 40) -> str:
    return "\n".join(
        f"{e.id:>5}  {e.type:<20} {(e.message or '').replace(chr(10), ' ')[:110]}"
        for e in events[-limit:]
    )


async def load_run_bundle(run_id: str) -> dict[str, Any] | None:
    """Everything the report needs, in one read."""
    async with session_scope() as session:
        result = await session.execute(
            select(Run)
            .where(Run.id == run_id)
            .options(
                selectinload(Run.steps),
                selectinload(Run.approvals),
                selectinload(Run.screenshots),
                selectinload(Run.task),
            )
        )
        run = result.scalars().first()
        if run is None:
            return None
        events = (
            await session.execute(
                select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.id)
            )
        ).scalars().all()
        return {
            "run": run,
            "task": run.task,
            "steps": sorted(run.steps, key=lambda s: s.index),
            "approvals": sorted(run.approvals, key=lambda a: a.requested_at),
            "screenshots": sorted(run.screenshots, key=lambda s: s.sequence),
            "events": list(events),
        }


def render_markdown(bundle: dict[str, Any]) -> str:
    run: Run = bundle["run"]
    task = bundle["task"]
    steps: list[Step] = bundle["steps"]
    approvals: list[Approval] = bundle["approvals"]
    shots: list[Screenshot] = bundle["screenshots"]
    events: list[RunEvent] = bundle["events"]

    duration_s = (run.duration_ms or 0) / 1000
    baseline = run.manual_baseline_seconds
    saved = (baseline - duration_s) if baseline else None

    lines: list[str] = [
        f"# TaskPilot run - {task.title if task else run.goal}",
        "",
        f"> {run.goal}",
        "",
        "## Summary",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Status | **{_status_label(run.status)}** |",
        f"| Run id | `{run.id}` |",
        f"| Started | {run.started_at or '-'} |",
        f"| Duration | {_fmt_duration(duration_s)} |",
        f"| Manual baseline | {_fmt_duration(baseline)} |",
        f"| Time saved | {_fmt_duration(saved)} |",
        f"| Steps | {run.steps_succeeded}/{run.steps_total} succeeded |",
        f"| Approvals requested | {len(approvals)} |",
        f"| Replans | {run.replan_count} |",
        f"| Model | {run.provider} / {run.model} |",
        "",
    ]

    result = run.result or {}
    answer = result.get("final_answer") or ""
    if answer:
        lines += ["## Result", "", answer, ""]

    if run.error:
        lines += [
            "## Why it stopped",
            "",
            f"**{run.error.get('code', 'error')}** - {run.error.get('message', '')}",
            "",
        ]

    lines += [
        "## Steps",
        "",
        "| # | Step | Tool | Risk | Approval | Status | Time |",
        "|---|---|---|---|---|---|---|",
    ]
    for step in steps:
        icon = STATUS_ICON.get(step.status, "...")
        lines.append(
            f"| {step.index + 1} | {icon} {_cell(step.goal)} | `{step.tool}` | "
            f"{step.effective_risk} | {'required' if step.needs_approval else '-'} | "
            f"{step.status} | {_fmt_ms(step.latency_ms)} |"
        )
    lines.append("")

    detailed = [s for s in steps if s.observation]
    if detailed:
        lines += ["### Step detail", ""]
        for step in detailed:
            observation = step.observation or {}
            lines += [
                f"#### {step.index + 1}. {step.goal}",
                "",
                f"- **Tool**: `{step.tool}`",
                f"- **Args**: `{json.dumps(_public_args(step), default=str)[:300]}`",
                f"- **Result**: {observation.get('summary') or '-'}",
            ]
            if step.error:
                lines.append(f"- **Error**: {(step.error or {}).get('message', '-')}")
            if step.verification:
                lines.append(
                    f"- **Verified**: {step.verification.get('success')} "
                    f"({step.verification.get('reason', '')})"
                )
            if observation.get("screenshots"):
                lines.append(f"- **Screenshots**: {', '.join(observation['screenshots'])}")
            lines.append("")

    if approvals:
        lines += ["## Approvals", "", "| Step | Risk | Decision | By | When |", "|---|---|---|---|---|"]
        for approval in approvals:
            step = next((s for s in steps if s.id == approval.step_id), None)
            step_no = (step.index + 1) if step else "?"
            label = DECISION_LABEL.get(approval.status, approval.status)
            lines.append(
                f"| {step_no} | {approval.risk} | {label} | {approval.decided_by or '-'} | "
                f"{approval.decided_at or approval.requested_at} |"
            )
        lines.append("")
        for approval in approvals:
            explanation = (approval.preview or {}).get("explanation")
            if explanation:
                lines.append(f"- `{approval.step_id[:8]}`: {explanation}")
        lines.append("")

    if shots:
        lines += ["## Screenshots", ""]
        for shot in shots:
            lines.append(f"- `{shot.path}` - {shot.url or ''}")
        lines.append("")

    lines += [
        "## Audit trail",
        "",
        f"{len(events)} events recorded (immutable; replay with "
        f"`GET /api/runs/{run.id}/events`).",
        "",
        "```text",
        _event_tail(events),
        "```",
        "",
        "---",
        "",
        f"Generated by TaskPilot at "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}. "
        "Every action above is reproducible from the event log.",
    ]
    return "\n".join(lines)


def render_json(bundle: dict[str, Any]) -> dict[str, Any]:
    """Machine-readable report: the same facts, shaped for the eval harness."""
    run: Run = bundle["run"]
    steps: list[Step] = bundle["steps"]
    approvals: list[Approval] = bundle["approvals"]
    duration_s = (run.duration_ms or 0) / 1000
    granted = [
        a
        for a in approvals
        if a.status in {ApprovalStatus.APPROVED.value, ApprovalStatus.EDITED.value}
    ]
    risky_executed = [
        s
        for s in steps
        if s.needs_approval and s.status == StepStatus.DONE.value and not s.approved_at
    ]
    return {
        "run_id": run.id,
        "task_id": run.task_id,
        "goal": run.goal,
        "status": run.status,
        "provider": run.provider,
        "model": run.model,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "duration_seconds": round(duration_s, 3),
        "manual_baseline_seconds": run.manual_baseline_seconds,
        "time_saved_seconds": (
            round(run.manual_baseline_seconds - duration_s, 3)
            if run.manual_baseline_seconds
            else None
        ),
        "final_answer": (run.result or {}).get("final_answer", ""),
        "error": run.error,
        "replans": run.replan_count,
        "step_accuracy": (
            round(len([s for s in steps if s.status == StepStatus.DONE.value]) / len(steps), 4)
            if steps
            else 0.0
        ),
        "steps": [
            {
                "index": s.index,
                "tool": s.tool,
                "goal": s.goal,
                "status": s.status,
                "attempts": s.attempts,
                "risk": s.effective_risk,
                "needs_approval": s.needs_approval,
                "approved_at": s.approved_at.isoformat() if s.approved_at else None,
                "latency_ms": s.latency_ms,
                "summary": (s.observation or {}).get("summary"),
                "extracted": s.extracted,
                "error": s.error,
            }
            for s in steps
        ],
        "approvals": [
            {
                "step_id": a.step_id,
                "status": a.status,
                "risk": a.risk,
                "decided_by": a.decided_by,
                "decided_at": a.decided_at.isoformat() if a.decided_at else None,
                "note": a.decision_note,
                "explanation": (a.preview or {}).get("explanation"),
                "original_args": a.original_args,
                "edited_args": a.edited_args,
            }
            for a in approvals
        ],
        "gate_integrity": {
            "risky_steps": len([s for s in steps if s.needs_approval]),
            "approvals_requested": len(approvals),
            "approvals_granted": len(granted),
            "risky_actions_executed_without_approval": len(risky_executed),
        },
        "screenshots": [s.path for s in bundle["screenshots"]],
        "event_count": len(bundle["events"]),
    }


def render_pdf(bundle: dict[str, Any], path: Any) -> bool:
    """Write a PDF report. Returns False when reportlab is unavailable."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            PageBreak,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError:  # pragma: no cover - optional dependency
        logger.warning("reportlab is not installed; skipping PDF generation")
        return False

    run: Run = bundle["run"]
    steps: list[Step] = bundle["steps"]
    approvals: list[Approval] = bundle["approvals"]
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=17, spaceAfter=10)
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=12, spaceAfter=6)
    body = ParagraphStyle("Body", parent=styles["BodyText"], fontSize=9, leading=13)
    mono = ParagraphStyle("Mono", parent=styles["BodyText"], fontName="Courier", fontSize=7)
    grid = (
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D4D4D4")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F5F5F5")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
    )

    duration_s = (run.duration_ms or 0) / 1000
    story: list[Any] = [
        Paragraph("TaskPilot run report", h1),
        Paragraph(run.goal, body),
        Spacer(1, 8),
    ]
    summary = Table(
        [
            ["Status", _status_label(run.status)],
            ["Run id", run.id],
            ["Duration", _fmt_duration(duration_s)],
            ["Manual baseline", _fmt_duration(run.manual_baseline_seconds)],
            ["Steps", f"{run.steps_succeeded}/{run.steps_total} succeeded"],
            ["Approvals", f"{len(approvals)} requested"],
            ["Replans", str(run.replan_count)],
            ["Model", f"{run.provider} / {run.model}"],
        ],
        colWidths=[42 * mm, 123 * mm],
    )
    summary.setStyle(TableStyle(list(grid)))
    story += [summary, Spacer(1, 12)]

    answer = (run.result or {}).get("final_answer")
    if answer:
        story += [Paragraph("Result", h2), Paragraph(answer, body), Spacer(1, 8)]
    if run.error:
        story += [
            Paragraph("Why it stopped", h2),
            Paragraph(f"{run.error.get('code')}: {run.error.get('message')}", body),
            Spacer(1, 8),
        ]

    rows = [["#", "Step", "Tool", "Risk", "Approval", "Status"]]
    for step in steps:
        rows.append(
            [
                str(step.index + 1),
                _cell(step.goal, 58),
                _cell(step.tool, 25),
                step.effective_risk,
                "required" if step.needs_approval else "-",
                step.status,
            ]
        )
    step_table = Table(rows, colWidths=[8 * mm, 68 * mm, 32 * mm, 14 * mm, 20 * mm, 23 * mm])
    step_table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D4D4D4")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F5F5F5")),
                ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story += [Paragraph("Steps", h2), step_table]

    if approvals:
        story += [PageBreak(), Paragraph("Approvals", h2)]
        for approval in approvals:
            story += [
                Paragraph(
                    f"<b>{DECISION_LABEL.get(approval.status, approval.status)}</b> "
                    f"({approval.risk}) by {approval.decided_by or 'system'}",
                    body,
                ),
                Paragraph((approval.preview or {}).get("explanation", ""), body),
                Spacer(1, 6),
            ]
    if bundle["events"]:
        story += [
            Paragraph("Audit trail (last 40 events)", h2),
            Paragraph(_event_tail(bundle["events"]), mono),
        ]

    SimpleDocTemplate(
        str(path), pagesize=A4, title="TaskPilot run report", author="TaskPilot"
    ).build(story)
    return True


def report_paths(run_id: str) -> dict[str, Any]:
    settings = get_settings()
    base = settings.reports_path
    return {
        "dir": base,
        "markdown": base / f"{run_id}.md",
        "json": base / f"{run_id}.json",
        "pdf": base / f"{run_id}.pdf",
    }


async def write_report_files(run_id: str) -> dict[str, Any]:
    """Render and persist all three report formats for a finished run."""
    bundle = await load_run_bundle(run_id)
    if bundle is None:
        return {}
    paths = report_paths(run_id)
    markdown = render_markdown(bundle)
    payload = render_json(bundle)
    paths["markdown"].write_text(markdown, encoding="utf-8")
    paths["json"].write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    pdf_ok = render_pdf(bundle, paths["pdf"])
    log_event(
        logger,
        logging.INFO,
        "run report written",
        run_id=run_id,
        markdown=str(paths["markdown"].name),
        json=str(paths["json"].name),
        pdf=bool(pdf_ok),
    )
    return {
        "markdown_path": str(paths["markdown"]),
        "json_path": str(paths["json"]),
        "pdf_path": str(paths["pdf"]) if pdf_ok else None,
        "markdown": markdown,
        "json": payload,
    }


__all__ = [
    "load_run_bundle",
    "render_json",
    "render_markdown",
    "render_pdf",
    "report_paths",
    "write_report_files",
]
