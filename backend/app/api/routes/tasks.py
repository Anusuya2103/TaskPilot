"""Task submission: ``POST /api/tasks`` is the single entry point into TaskPilot.

The response is the run, so the frontend can navigate straight to
``/runs/{id}`` and open the SSE stream without a second round-trip.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import orchestrator_dep, rate_limit, require_api_key
from app.core.db import session_scope
from app.core.models import Run, Task
from app.core.schemas import RunOut, TaskCreate, TaskOut
from app.schemas_api import to_run_out

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.post(
    "",
    response_model=RunOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a task and start a run",
    dependencies=[Depends(require_api_key), Depends(rate_limit)],
)
async def create_task(payload: TaskCreate, orchestrator: Any = Depends(orchestrator_dep)) -> Any:
    """Create the task + run, then (unless parked for review) start executing."""
    run = await orchestrator.create_task_run(payload)
    if payload.auto_start and not payload.review_plan:
        await orchestrator.start_run(run.id)
    # `to_run_out` is async: re-read the run so the response includes the
    # persisted plan and steps that `start_run` may have created.
    return await to_run_out(run.id)


@router.get("", response_model=list[TaskOut], summary="List recent tasks")
async def list_tasks(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[TaskOut]:
    async with session_scope() as session:
        result = await session.execute(
            select(Task).order_by(Task.created_at.desc()).limit(limit).offset(offset)
        )
        return [TaskOut.model_validate(t) for t in result.scalars().all()]


@router.get("/{task_id}", response_model=TaskOut, summary="Fetch one task")
async def get_task(task_id: str) -> Any:
    from app.core.errors import NotFoundError

    async with session_scope() as session:
        task = await session.get(Task, task_id)
        if task is None:
            raise NotFoundError(f"Task {task_id!r} not found")
        return TaskOut.model_validate(task)


@router.get("/{task_id}/runs", response_model=list[RunOut], summary="Runs for a task")
async def task_runs(task_id: str, limit: int = Query(default=20, ge=1, le=100)) -> list[Any]:
    async with session_scope() as session:
        result = await session.execute(
            select(Run)
            .where(Run.task_id == task_id)
            .options(selectinload(Run.steps), selectinload(Run.task))
            .order_by(Run.created_at.desc())
            .limit(limit)
        )
        runs = result.scalars().all()
        return [await to_run_out(run, session=session) for run in runs]
