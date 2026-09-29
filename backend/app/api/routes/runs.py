"""Run lifecycle: read, list, cancel, decide approvals, plan review, reports."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select

from app.api.deps import orchestrator_dep, rate_limit, require_api_key
from app.core.db import session_scope
from app.core.errors import NotFoundError
from app.core.models import Approval, ApprovalStatus, Run, Screenshot
from app.core.schemas import (
    ApprovalDecision,
    ApprovalOut,
    CancelResponse,
    Paginated,
    ReportOut,
    RunListOut,
    RunOut,
    RunSummaryOut,
)
from app.schemas_api import to_approval_out, to_run_out, to_screenshot_out

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.get("", response_model=Paginated, summary="List runs")
async def list_runs(
    status_filter: str | None = Query(default=None, alias="status"),
    search: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> Any:
    async with session_scope() as session:
        stmt = select(Run)
        count_stmt = select(func.count()).select_from(Run)
        if status_filter and status_filter != "all":
            stmt = stmt.where(Run.status == status_filter)
            count_stmt = count_stmt.where(Run.status == status_filter)
        if search:
            pattern = f"%{search}%"
            clause = or_(Run.goal.ilike(pattern), Run.id.ilike(pattern))
            stmt = stmt.where(clause)
            count_stmt = count_stmt.where(clause)
        total = (await session.execute(count_stmt)).scalar_one()
        result = await session.execute(
            stmt.order_by(Run.created_at.desc()).limit(limit).offset(offset)
        )
        items = [
            RunListOut(
                id=r.id,
                task_id=r.task_id,
                status=r.status,
                goal=r.goal,
                prompt=r.task.prompt if r.task else "",
                created_at=r.created_at,
                finished_at=r.finished_at,
                duration_ms=r.duration_ms,
                steps_total=r.steps_total,
                steps_succeeded=r.steps_succeeded,
                approvals_count=r.approvals_count,
                error=r.error,
                manual_baseline_seconds=r.manual_baseline_seconds,
            )
            for r in result.scalars().all()
        ]
        return Paginated(items=items, total=total, limit=limit, offset=offset)


@router.get("/{run_id}", response_model=RunOut, summary="Fetch one run")
async def get_run(run_id: str) -> Any:
    return await to_run_out(run_id)


@router.get("/{run_id}/screenshots", summary="Screenshots for a run")
async def get_screenshots(run_id: str) -> dict[str, Any]:
    async with session_scope() as session:
        result = await session.execute(
            select(Screenshot).where(Screenshot.run_id == run_id).order_by(Screenshot.sequence)
        )
        shots = result.scalars().all()
        return {
            "run_id": run_id,
            "items": [to_screenshot_out(s).model_dump() for s in shots],
            "count": len(shots),
        }


@router.post(
    "/{run_id}/cancel",
    response_model=CancelResponse,
    summary="Cancel a run",
    dependencies=[Depends(require_api_key), Depends(rate_limit)],
)
async def cancel_run(run_id: str, orchestrator: Any = Depends(orchestrator_dep)) -> CancelResponse:
    # A missing run surfaces as 404 from the orchestrator's own error type.
    await orchestrator.get_run(run_id)
    accepted = await orchestrator.cancel_run(run_id)
    run = await orchestrator.get_run(run_id)
    return CancelResponse(
        run_id=run_id,
        status=run.status,
        accepted=accepted,
        message=(
            "Run cancelled. The agent stopped and released its browser."
            if accepted
            else "Run had already finished, so nothing was cancelled."
        ),
    )


@router.post(
    "/{run_id}/approvals/{step_id}",
    response_model=ApprovalOut,
    summary="Approve, reject, or edit-and-approve a step",
    dependencies=[Depends(require_api_key), Depends(rate_limit)],
)
async def decide_approval(
    run_id: str,
    step_id: str,
    decision: ApprovalDecision,
    orchestrator: Any = Depends(orchestrator_dep),
) -> Any:
    """The one endpoint a human uses to release a gated action.

    ``decision`` is ``approve`` | ``reject`` | ``edit_args``. The response is the
    recorded :class:`Approval` row — the audit record of what happened.
    """
    approval = await orchestrator.decide_approval(run_id, step_id, decision)
    return to_approval_out(approval)


@router.get("/{run_id}/approvals", response_model=list[ApprovalOut], summary="Approvals")
async def list_approvals(run_id: str) -> list[Any]:
    async with session_scope() as session:
        result = await session.execute(
            select(Approval).where(Approval.run_id == run_id).order_by(Approval.requested_at)
        )
        return [to_approval_out(a) for a in result.scalars().all()]


@router.post("/{run_id}/plan/approve", response_model=RunOut, summary="Approve the plan")
async def approve_plan(run_id: str, orchestrator: Any = Depends(orchestrator_dep)) -> Any:
    if not await orchestrator.approve_plan(run_id):
        raise NotFoundError(f"Run {run_id!r} is not waiting for plan review")
    return await to_run_out(run_id)


@router.post("/{run_id}/plan/reject", response_model=RunOut, summary="Reject the plan")
async def reject_plan(run_id: str, orchestrator: Any = Depends(orchestrator_dep)) -> Any:
    if not await orchestrator.reject_plan(run_id):
        raise NotFoundError(f"Run {run_id!r} is not waiting for plan review")
    return await to_run_out(run_id)


@router.get("/{run_id}/summary", response_model=RunSummaryOut, summary="Run summary")
async def run_summary(run_id: str) -> Any:
    """The completion-screen payload, including the gate-integrity check."""
    from app.reports.generator import load_run_bundle, render_json

    bundle = await load_run_bundle(run_id)
    if bundle is None:
        raise NotFoundError(f"Run {run_id!r} not found")
    payload = render_json(bundle)
    gate = payload["gate_integrity"]
    steps = bundle["steps"]
    return RunSummaryOut(
        run_id=run_id,
        status=payload["status"],
        goal=payload["goal"],
        output=payload["final_answer"],
        steps_total=len(steps),
        steps_succeeded=len([s for s in steps if s.status == "done"]),
        steps_failed=len([s for s in steps if s.status == "failed"]),
        steps_skipped=len([s for s in steps if s.status == "skipped"]),
        step_accuracy=payload["step_accuracy"],
        steps_requiring_approval=gate["risky_steps"],
        approvals_requested=gate["approvals_requested"],
        approvals_granted=gate["approvals_granted"],
        approvals_rejected=len(
            [a for a in bundle["approvals"] if a.status == ApprovalStatus.REJECTED.value]
        ),
        approvals_timed_out=len(
            [a for a in bundle["approvals"] if a.status == ApprovalStatus.TIMED_OUT.value]
        ),
        risky_actions_executed_without_approval=gate["risky_actions_executed_without_approval"],
        duration_seconds=payload["duration_seconds"],
        manual_baseline_seconds=payload["manual_baseline_seconds"],
        time_saved_seconds=payload["time_saved_seconds"],
        extracted={f"step{s.index + 1}": s.extracted for s in steps if s.extracted},
        screenshots=payload["screenshots"],
        replans=payload["replans"],
    )


@router.get("/{run_id}/report", response_model=ReportOut, summary="Run report")
async def get_report(run_id: str) -> Any:
    from app.reports.generator import load_run_bundle, render_json, render_markdown

    bundle = await load_run_bundle(run_id)
    if bundle is None:
        raise NotFoundError(f"Run {run_id!r} not found")
    return ReportOut(
        run_id=run_id,
        markdown=render_markdown(bundle),
        json_report=render_json(bundle),
        download_urls={
            "markdown": f"/api/runs/{run_id}/report.md",
            "json": f"/api/runs/{run_id}/report.json",
            "pdf": f"/api/runs/{run_id}/report.pdf",
        },
    )


@router.get("/{run_id}/report.{fmt}", summary="Download a report file")
async def download_report(run_id: str, fmt: str) -> Any:
    """``fmt`` is ``md`` | ``json`` | ``pdf``. Files are rendered on demand."""
    import json as jsonlib

    from app.reports.generator import (
        load_run_bundle,
        render_json,
        render_markdown,
        render_pdf,
        report_paths,
    )

    key = fmt.lower()
    if key not in {"md", "markdown", "json", "pdf"}:
        raise NotFoundError(f"Unknown report format {fmt!r}")
    paths = report_paths(run_id)
    target = {
        "md": paths["markdown"],
        "markdown": paths["markdown"],
        "json": paths["json"],
        "pdf": paths["pdf"],
    }[key]
    if not target.exists():
        bundle = await load_run_bundle(run_id)
        if bundle is None:
            raise NotFoundError(f"Run {run_id!r} not found")
        paths["markdown"].write_text(render_markdown(bundle), encoding="utf-8")
        paths["json"].write_text(
            jsonlib.dumps(render_json(bundle), indent=2, default=str), encoding="utf-8"
        )
        render_pdf(bundle, paths["pdf"])
    if not target.exists():
        raise NotFoundError(f"Report for run {run_id!r} is not available")
    media = {".md": "text/markdown", ".json": "application/json", ".pdf": "application/pdf"}[
        target.suffix
    ]
    return FileResponse(target, media_type=media, filename=target.name)


__all__ = ["router"]
