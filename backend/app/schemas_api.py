"""Serialisation helpers shared by the API routes.

Kept in one module so the "what does the API return" decision has a single
answer, and so :class:`RunOut` is assembled the same way everywhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import session_scope
from app.core.errors import NotFoundError
from app.core.models import Approval, ApprovalStatus, Run
from app.core.schemas import ApprovalOut, RunOut, ScreenshotOut, StepOut
from app.core.logging import scrub


async def _load_run(session: AsyncSession, run_id: str) -> Run:
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
        raise NotFoundError(f"Run {run_id!r} not found")
    return run


def _pending_approval(run: Run) -> Approval | None:
    for approval in run.approvals or []:
        if approval.status == ApprovalStatus.PENDING.value:
            return approval
    return None


def to_approval_out(approval: Approval) -> ApprovalOut:
    payload = ApprovalOut.model_validate(approval)
    if approval.expires_at is not None and approval.requested_at is not None:
        payload.seconds_remaining = max(
            0, int((approval.expires_at - approval.requested_at).total_seconds())
        )
    return payload


async def to_run_out(run: Run | str, *, session: AsyncSession | None = None) -> RunOut:
    """Assemble a :class:`RunOut`, loading the run by id when needed.

    Passing the ORM object avoids a second query when the caller already has it.
    """
    if isinstance(run, str):
        async with session_scope() as new_session:
            loaded = await _load_run(new_session, run)
            # `await` is essential: `return to_run_out(loaded)` from inside an
            # `async def` hands the caller a *coroutine*, not a RunOut, and
            # FastAPI then fails response validation.
            return await to_run_out(loaded)
    prompt = run.task.prompt if run.task else ""
    pending = _pending_approval(run)
    return RunOut(
        id=run.id,
        task_id=run.task_id,
        status=run.status,
        goal=run.goal,
        plan=scrub(run.plan) if run.plan else None,
        plan_revision=run.plan_revision,
        replan_count=run.replan_count,
        provider=run.provider,
        model=run.model,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_ms=run.duration_ms,
        manual_baseline_seconds=run.manual_baseline_seconds,
        steps_total=run.steps_total,
        steps_succeeded=run.steps_succeeded,
        approvals_count=run.approvals_count,
        result=run.result,
        error=run.error,
        cancel_requested=run.cancel_requested,
        steps=[
            StepOut(
                id=s.id,
                run_id=s.run_id,
                index=s.index,
                goal=s.goal,
                tool=s.tool,
                args={k: v for k, v in (s.args or {}).items() if not k.startswith("__")},
                expected_outcome=s.expected_outcome,
                risk=s.risk,
                declared_risk=s.declared_risk,
                effective_risk=s.effective_risk,
                needs_approval=s.needs_approval,
                risk_reasons=s.risk_reasons or [],
                status=s.status,
                attempts=s.attempts,
                observation=scrub(s.observation) if s.observation else None,
                extracted=scrub(s.extracted) if s.extracted else None,
                verification=s.verification,
                error=s.error,
                started_at=s.started_at,
                finished_at=s.finished_at,
                latency_ms=s.latency_ms,
                approved_at=s.approved_at,
            )
            for s in sorted(run.steps or [], key=lambda s: s.index)
        ],
        pending_approval=to_approval_out(pending) if pending else None,
        prompt=prompt,
    )


def to_screenshot_out(shot: Any) -> ScreenshotOut:
    out = ScreenshotOut.model_validate(shot)
    path = str(shot.path)
    out.src = path if path.startswith("/") else f"/media/screenshots/{Path(path).name}"
    return out


__all__ = ["to_approval_out", "to_run_out", "to_screenshot_out"]
