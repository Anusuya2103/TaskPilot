"""The orchestrator: the only component that knows about both DB and agent.

Responsibilities:

* create a :class:`~app.core.models.Run` and kick off its background task,
* translate every executor hook into a persisted row **and** a live SSE event
  (one code path, so the audit trail and the live feed can never disagree),
* own cancellation, and
* accept approval decisions from the API and hand them to the broker.

The run executes as an ``asyncio.Task`` owned by this process. That is a
deliberate v1 trade-off: a paused run holds an in-memory task, so a process
restart loses in-flight work. The audit trail survives, the run is marked
interrupted on boot, and the fix (a durable queue) is noted in the README's
limitations.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agent.approvals import get_broker
from app.agent.executor import Executor, RunHooks, RunOutcome
from app.agent.llm import get_llm
from app.agent.planner import PlanResult, PlannedStep, Planner
from app.agent.verifier import Verifier
from app.core.config import Settings, get_settings
from app.core.db import get_sessionmaker, session_scope
from app.core.errors import ConflictError, NotFoundError, RunCancelled, TaskPilotError
from app.core.events import append_event, get_bus
from app.core.logging import bind_run, get_logger, log_event
from app.core.models import (
    Approval,
    ApprovalStatus,
    EventType,
    Run,
    RunStatus,
    Screenshot,
    Step,
    StepStatus,
    Task,
    new_id,
    utcnow,
)
from app.core.schemas import ApprovalDecision, Plan, TaskCreate
from app.core.security import sanitize_untrusted
from app.tools import register_builtin_tools
from app.tools.browser import get_browser_manager
from app.tools.registry import get_registry

logger = get_logger(__name__)


class RunManager:
    """In-process registry of running tasks, approvals and cancellation flags."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._cancelled: set[str] = set()
        self._lock = asyncio.Lock()
        self.broker = get_broker()

    # --- lifecycle ------------------------------------------------------------
    def is_cancelled(self, run_id: str) -> bool:
        return run_id in self._cancelled

    def cancel(self, run_id: str) -> bool:
        if run_id in self._cancelled:
            return False
        self._cancelled.add(run_id)
        self.broker.cancel_run(run_id)
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
        return True

    def is_running(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        return task is not None and not task.done()

    def active_run_ids(self) -> list[str]:
        return [rid for rid, t in self._tasks.items() if not t.done()]

    def forget(self, run_id: str) -> None:
        self._tasks.pop(run_id, None)
        self._cancelled.discard(run_id)


_manager: RunManager | None = None


def get_manager() -> RunManager:
    global _manager
    if _manager is None:
        _manager = RunManager()
    return _manager


def reset_manager() -> None:
    """Test helper."""
    global _manager
    if _manager is not None:
        for task in _manager._tasks.values():
            task.cancel()
    _manager = None


class Orchestrator:
    """Creates, executes and finalises runs."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.manager = get_manager()
        # run_id -> {plan_step_id: {"db_id": str, "index": int}}
        self._step_index: dict[str, dict[str, dict[str, Any]]] = {}
        register_builtin_tools()

    # --- creation -------------------------------------------------------------
    async def create_task_run(self, payload: TaskCreate) -> Run:
        """Persist the task + a planning run, emit the first event."""
        prompt = payload.prompt.strip()
        async with session_scope() as session:
            task = Task(
                prompt=prompt,
                title=payload.title or _title_from_prompt(prompt),
                source=payload.source,
                tags=payload.tags,
            )
            session.add(task)
            await session.flush()

            run = Run(
                task_id=task.id,
                status=RunStatus.PLANNING.value,
                goal=prompt,
                provider=payload.provider or self.settings.llm_provider,
                model=payload.model or self.settings.llm_model,
                params={
                    "auto_start": payload.auto_start,
                    "review_plan": payload.review_plan,
                    "max_steps": payload.max_steps,
                    "auto_approve_low_risk": payload.auto_approve_low_risk,
                    **payload.metadata,
                },
            )
            session.add(run)
            await session.flush()
            run_id, task_id = run.id, task.id

        await self._emit(
            run_id,
            EventType.RUN_CREATED,
            "Task received",
            {"task_id": task_id, "prompt": prompt},
        )
        return await self.get_run(run_id)

    async def start_run(self, run_id: str) -> bool:
        """Schedule the run. Idempotent: an already-running run is not restarted."""
        if self.manager.is_running(run_id):
            return False
        run = await self.get_run(run_id)
        if run.status in {s.value for s in RunStatus.terminal()}:
            return False
        task = asyncio.create_task(self._execute(run_id), name=f"taskpilot-run-{run_id[:8]}")
        self.manager._tasks[run_id] = task
        task.add_done_callback(lambda _t: self.manager._tasks.pop(run_id, None))
        return True

    async def get_run(self, run_id: str) -> Run:
        async with session_scope() as session:
            return await self._load_run(session, run_id)

    async def _load_run(self, session: AsyncSession, run_id: str) -> Run:
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

    # --- execution ------------------------------------------------------------
    async def _execute(self, run_id: str) -> None:
        bind_run(run_id)
        started = time.perf_counter()
        state: dict[str, Any] = {}
        try:
            async with session_scope() as session:
                run = await self._load_run(session, run_id)
                run.status = RunStatus.PLANNING.value
                run.started_at = utcnow()
                goal = run.goal
                params = dict(run.params or {})
                provider = run.provider or self.settings.llm_provider
                model = run.model or self.settings.llm_model
                review_plan = bool(params.get("review_plan"))
                max_steps = params.get("max_steps")

            await self._emit(
                run_id, EventType.RUN_STARTED, "Planning started",
                {"provider": provider, "model": model},
            )

            llm = get_llm(provider, model, self.settings)
            planner = Planner(llm, get_registry(), self.settings)
            plan_result = await planner.create_plan(goal, max_steps=max_steps)
            state = await self._persist_plan(run_id, plan_result, goal)

            if review_plan:
                await self._set_status(run_id, RunStatus.AWAITING_PLAN_APPROVAL)
                await self._emit(
                    run_id, EventType.PLAN_CREATED, "Plan ready for review",
                    {"steps": len(plan_result.steps),
                     "requires_approval": plan_result.requires_approval},
                )
                return

            await self._run_executor(run_id, goal, plan_result, state, started)
        except asyncio.CancelledError:
            await self._finish_cancelled(run_id, started)
            raise
        except RunCancelled as exc:
            await self._finish_cancelled(run_id, started, reason=str(exc))
        except TaskPilotError as exc:
            await self._finish_failed(run_id, started, exc)
        except Exception as exc:  # noqa: BLE001 - a background task must never vanish
            logger.exception("run %s crashed", run_id)
            await self._finish_failed(
                run_id, started, TaskPilotError(f"internal error: {type(exc).__name__}: {exc}")
            )
        finally:
            with contextlib.suppress(Exception):
                await get_browser_manager().release(run_id)

    async def _run_executor(
        self,
        run_id: str,
        goal: str,
        plan_result: PlanResult,
        state: dict[str, Any],
        started: float,
    ) -> None:
        await self._set_status(run_id, RunStatus.RUNNING)
        llm = get_llm(plan_result.provider or None, plan_result.model or None, self.settings)
        executor = Executor(
            registry=get_registry(),
            verifier=Verifier(llm),
            planner=Planner(llm, get_registry(), self.settings),
            broker=self.manager.broker,
            settings=self.settings,
            hooks=self._build_hooks(run_id, state),
        )
        outcome = await executor.run_plan(
            run_id=run_id,
            steps=plan_result.steps,
            goal=goal,
            state=state,
            is_cancelled=lambda: self.manager.is_cancelled(run_id),
            session_factory=get_sessionmaker(),
        )
        await self._persist_outcome(run_id, outcome, state, started)
        await self._finalise(run_id, outcome, state, started)

    # --- hooks: executor activity -> rows + events ---------------------------
    def _build_hooks(self, run_id: str, state: dict[str, Any]) -> RunHooks:
        hooks = RunHooks()

        async def db_step(plan_step_id: str) -> str | None:
            """DB id for a plan step id, creating the row if a replan introduced it."""
            return await self._ensure_step(run_id, plan_step_id)

        async def step_start(step: Any, _decision: Any = None) -> None:
            await self._set_step_status(run_id, step.id, StepStatus.RUNNING)
            await self._emit(
                run_id, EventType.STEP_STARTED, step.goal,
                {"step_id": step.id, "tool": step.tool, "args": step.args,
                 "expected_outcome": step.expected_outcome},
                step_id=await db_step(step.id),
            )

        async def tool_call(step: Any, args: dict[str, Any], attempt: int, total: int) -> None:
            await self._emit(
                run_id, EventType.TOOL_CALL, f"{step.tool} (attempt {attempt}/{total})",
                {"step_id": step.id, "tool": step.tool, "args": args},
                step_id=await db_step(step.id),
            )

        async def observation(step: Any, result: Any, verdict: Any) -> None:
            state["last_result"] = result.model_dump()
            if result.url:
                state["current_url"] = result.url
            await self._record_screenshots(run_id, step.id, result.screenshots, state)
            await self._emit(
                run_id, EventType.OBSERVATION, result.summary,
                {
                    "step_id": step.id,
                    "ok": result.ok,
                    "summary": result.summary,
                    "extracted": result.extracted,
                    "url": result.url,
                    "latency_ms": result.latency_ms,
                    "screenshots": result.screenshots,
                },
                step_id=await db_step(step.id),
            )
            await self._emit(
                run_id, EventType.VERIFICATION,
                "verified" if verdict.success else "verification failed",
                {"step_id": step.id, "success": verdict.success,
                 "reason": verdict.reason, "confidence": verdict.confidence},
                step_id=await db_step(step.id),
            )

        async def step_done(step: Any, outcome: Any) -> None:
            await self._finish_step(run_id, step.id, StepStatus.DONE, outcome)
            await self._emit(
                run_id, EventType.STEP_COMPLETED, step.goal,
                {"step_id": step.id, "attempts": outcome.attempts,
                 "latency_ms": outcome.latency_ms,
                 "summary": outcome.result.summary if outcome.result else None},
                step_id=await db_step(step.id),
            )

        async def step_failed(step: Any, outcome: Any) -> None:
            await self._finish_step(run_id, step.id, StepStatus.FAILED, outcome)
            await self._emit(
                run_id, EventType.STEP_FAILED, outcome.error or "step failed",
                {"step_id": step.id, "attempts": outcome.attempts, "error": outcome.error},
                step_id=await db_step(step.id),
            )

        async def step_skipped(step: Any, approval: Any) -> None:
            await self._finish_step(
                run_id, step.id, StepStatus.SKIPPED, None,
                reason=f"approval {approval.status.value}",
            )
            await self._emit(
                run_id, EventType.STEP_SKIPPED,
                f"skipped: approval {approval.status.value}",
                {"step_id": step.id, "status": approval.status.value},
                step_id=await db_step(step.id),
            )

        async def retry(step: Any, attempt: int, total: int, reason: str) -> None:
            await self._emit(
                run_id, EventType.RETRY, f"retrying {step.id} after failure",
                {"step_id": step.id, "attempt": attempt, "max_attempts": total,
                 "reason": reason},
                step_id=await db_step(step.id),
            )

        async def replan(failed: Any, reason: str, result: Any, number: int) -> None:
            await self._emit(
                run_id, EventType.REPLAN, f"replanned after {failed.id} failed",
                {"failed_step": failed.id, "reason": reason, "replan": number,
                 "new_steps": [s.step.goal for s in result.steps]},
            )

        async def approval_request(
            step: Any, decision: Any, preview: dict[str, Any], timeout: float
        ) -> dict[str, Any]:
            return await self._create_approval(run_id, step, decision, preview, timeout)

        async def approval_decision(step: Any, outcome: Any, approval_id: str | None) -> None:
            await self._record_approval_decision(run_id, step, outcome, approval_id)

        async def policy(step: Any, decision: Any, escalated: bool) -> None:
            await self._emit(
                run_id, EventType.POLICY_DECISION, decision.explanation()[:200],
                {"step_id": step.id, **decision.to_dict(), "escalated_by_rules": escalated},
                step_id=await db_step(step.id),
            )

        async def security(step: Any, reasons: list[str]) -> None:
            await self._emit(
                run_id, EventType.SECURITY_NOTICE,
                f"untrusted content filtered: {', '.join(reasons[:3])}",
                {"step_id": step.id, "flags": reasons},
                step_id=await db_step(step.id),
            )

        async def screenshot(step: Any, _event: Any, message: str, payload: dict[str, Any]) -> None:
            await self._emit(
                run_id, EventType.SCREENSHOT, message,
                {"step_id": step.id, **payload}, step_id=await db_step(step.id),
            )

        async def log(step: Any, _event: Any, message: str, payload: dict[str, Any]) -> None:
            await self._emit(
                run_id, EventType.LOG, message,
                {"step_id": step.id, **payload}, step_id=await db_step(step.id),
            )

        hooks.on_step_start = step_start
        hooks.on_tool_call = tool_call
        hooks.on_observation = observation
        hooks.on_step_done = step_done
        hooks.on_step_failed = step_failed
        hooks.on_step_skipped = step_skipped
        hooks.on_retry = retry
        hooks.on_replan = replan
        hooks.on_approval_request = approval_request
        hooks.on_approval_decision = approval_decision
        hooks.on_policy = policy
        hooks.on_security = security
        hooks.on_screenshot = screenshot
        hooks.on_log = log
        return hooks

    # --- persistence helpers --------------------------------------------------
    def _remember_step(self, run_id: str, plan_step_id: str, db_id: str, index: int) -> None:
        self._step_index.setdefault(run_id, {})[plan_step_id] = {"db_id": db_id, "index": index}

    async def _ensure_step(self, run_id: str, plan_step_id: str) -> str | None:
        """Return the DB id for a plan step id, creating the row when needed.

        Replans introduce step ids that were never persisted at plan time, so the
        row is materialised on first reference.
        """
        known = self._step_index.get(run_id, {}).get(plan_step_id)
        if known:
            return known["db_id"]
        async with session_scope() as session:
            step = await _find_step(session, run_id, plan_step_id)
            if step is None:
                existing = await session.execute(
                    select(Step.index).where(Step.run_id == run_id)
                )
                next_index = len(existing.scalars().all())
                step = Step(
                    run_id=run_id,
                    index=next_index,
                    goal=plan_step_id,
                    tool="",
                    args={"__plan_step_id": plan_step_id},
                    status=StepStatus.PENDING.value,
                )
                session.add(step)
                await session.flush()
            db_id = step.id
            index = step.index
        self._remember_step(run_id, plan_step_id, db_id, index)
        return db_id

    async def _emit(
        self,
        run_id: str,
        event: EventType,
        message: str = "",
        payload: dict[str, Any] | None = None,
        *,
        step_id: str | None = None,
    ) -> None:
        """Persist an event and push it to live subscribers, in one place."""
        async with session_scope() as session:
            await append_event(
                session, run_id, event, message=message, payload=payload, step_id=step_id
            )

    async def _set_status(self, run_id: str, status: RunStatus) -> None:
        async with session_scope() as session:
            run = await session.get(Run, run_id)
            if run is not None:
                run.status = status.value

    async def _set_step_status(self, run_id: str, plan_step_id: str, status: StepStatus) -> None:
        db_id = await self._ensure_step(run_id, plan_step_id)
        async with session_scope() as session:
            step = await session.get(Step, db_id) if db_id else None
            if step is not None:
                step.status = status.value
                if status is StepStatus.RUNNING and step.started_at is None:
                    step.started_at = utcnow()

    async def _finish_step(
        self,
        run_id: str,
        plan_step_id: str,
        status: StepStatus,
        outcome: Any = None,
        *,
        reason: str | None = None,
    ) -> None:
        db_id = await self._ensure_step(run_id, plan_step_id)
        if not db_id:
            return
        async with session_scope() as session:
            row = await session.get(Step, db_id)
            if row is None:  # pragma: no cover - defensive
                return
            row.status = status.value
            row.finished_at = utcnow()
            if outcome is not None:
                row.attempts = max(row.attempts, outcome.attempts)
                row.latency_ms = outcome.latency_ms
                if outcome.result is not None:
                    row.observation = {
                        "summary": outcome.result.summary,
                        "ok": outcome.result.ok,
                        "data": _trim(outcome.result.data),
                        "error": outcome.result.error,
                        "screenshots": outcome.result.screenshots,
                        "reasons": outcome.result.reasons,
                    }
                    row.extracted = _trim(outcome.result.extracted)
                if outcome.verification is not None:
                    row.verification = outcome.verification.model_dump()
            if reason or (outcome is not None and outcome.error):
                row.error = {"code": status.value, "message": reason or outcome.error}

    async def _persist_plan(
        self, run_id: str, result: PlanResult, goal: str
    ) -> dict[str, Any]:
        """Write the plan + step rows, and emit ``plan_created`` with the policy view."""
        state: dict[str, Any] = {}
        async with session_scope() as session:
            run = await session.get(Run, run_id)
            if run is None:  # pragma: no cover
                return state
            run.plan = result.to_dict()
            run.goal = result.plan.goal or goal
            run.manual_baseline_seconds = (
                result.plan.estimated_manual_minutes * 60
                if result.plan.estimated_manual_minutes
                else None
            )
            run.steps_total = len(result.steps)
            run.plan_revision = 1

            for planned in result.steps:
                step = Step(
                    run_id=run_id,
                    index=planned.index,
                    goal=planned.step.goal,
                    tool=planned.step.tool,
                    # The plan id is stored inside args so the API can address the
                    # step as "s1" while the primary key stays a UUID.
                    args={"__plan_step_id": planned.step.id, **planned.step.args},
                    expected_outcome=planned.step.expected_outcome,
                    risk=planned.step.risk,
                    declared_risk=planned.decision.declared_risk.value,
                    effective_risk=planned.decision.effective_risk.value,
                    needs_approval=planned.decision.needs_approval,
                    risk_reasons=[r.to_dict() for r in planned.decision.reasons],
                    status=StepStatus.PENDING.value,
                )
                session.add(step)
                await session.flush()
                self._remember_step(run_id, planned.step.id, step.id, step.index)

        await self._emit(
            run_id,
            EventType.PLAN_CREATED,
            f"Plan ready: {len(result.steps)} step(s)",
            {
                "goal": result.plan.goal,
                "steps": [s.to_dict() for s in result.steps],
                "warnings": result.warnings,
                "requires_approval": result.requires_approval,
                "highest_risk": result.highest_risk.value,
                "success_criteria": result.plan.success_criteria,
                "latency_ms": result.latency_ms,
                "provider": result.provider,
                "model": result.model,
            },
        )
        return state

    # --- approvals ------------------------------------------------------------
    async def _create_approval(
        self,
        run_id: str,
        step: Any,
        decision: Any,
        preview: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        """Persist the pending approval and put the run into ``awaiting_approval``."""
        db_step_id = await self._ensure_step(run_id, step.id)
        expires_at = utcnow() + timedelta(seconds=timeout)
        async with session_scope() as session:
            approval = Approval(
                run_id=run_id,
                step_id=db_step_id,
                status=ApprovalStatus.PENDING.value,
                reason=decision.explanation()[:1000],
                risk=decision.effective_risk.value,
                risk_reasons=[r.to_dict() for r in decision.reasons],
                preview=preview,
                original_args=step.args,
                screenshot_url=preview.get("screenshot"),
                expires_at=expires_at,
                channel="ui",
            )
            session.add(approval)
            run = await session.get(Run, run_id)
            if run is not None:
                run.status = RunStatus.AWAITING_APPROVAL.value
                run.approvals_count += 1
            step_row = await session.get(Step, db_step_id) if db_step_id else None
            if step_row is not None:
                step_row.status = StepStatus.AWAITING_APPROVAL.value
            await session.flush()
            approval_id = approval.id

        await self._emit(
            run_id,
            EventType.APPROVAL_REQUESTED,
            preview.get("action") or step.goal,
            {
                "approval_id": approval_id,
                "step_id": step.id,
                "risk": decision.effective_risk.value,
                "reasons": [r.to_dict() for r in decision.reasons],
                "preview": preview,
                "expires_at": expires_at.isoformat(),
                "timeout_seconds": timeout,
            },
            step_id=db_step_id,
        )
        return {"approval_id": approval_id, "expires_at": expires_at}

    async def decide_approval(
        self,
        run_id: str,
        step_id: str,
        decision: ApprovalDecision,
    ) -> Approval:
        """API entry point: record a human decision and release the executor.

        ``step_id`` may be either the plan id (``s8``) or the step UUID — the
        approval card uses the plan id, but the API surface also exposes the row
        id, and both must release the *same* waiter. The broker is keyed on the
        plan id, so it is resolved here.
        """
        async with session_scope() as session:
            step = await _find_step(session, run_id, step_id)
            if step is None:
                raise NotFoundError(f"No step {step_id!r} in run {run_id!r}")
            approval = await _latest_pending_approval(session, run_id, step.id)
            if approval is None:
                raise ConflictError(
                    "There is no pending approval for that step — it may already be "
                    "decided, or the step never required one.",
                    details={"step_id": step_id},
                )

            status = {
                "approve": ApprovalStatus.APPROVED,
                "reject": ApprovalStatus.REJECTED,
                "edit_args": ApprovalStatus.EDITED,
            }[decision.decision]
            approval.status = status.value
            approval.decided_at = utcnow()
            approval.decided_by = decision.decided_by
            approval.decision_note = decision.note
            if decision.decision == "edit_args":
                approval.edited_args = decision.edited_args

            effective = approval.effective_args
            if status.granted:
                step.approved_at = approval.decided_at
            approval_id = approval.id
            db_step_id = step.id
            plan_step_id = _plan_step_id_of(step)

        # Wake the executor *after* the row is committed, so the DB poll and the
        # in-process event can never disagree about what was decided. The broker
        # is keyed by the plan step id, which is what the executor is waiting on.
        self.manager.broker.decide(
            run_id=run_id,
            step_id=plan_step_id,
            status=status,
            args=effective,
            decided_by=decision.decided_by,
            note=decision.note,
            approval_id=approval_id,
        )
        await self._emit(
            run_id,
            EventType.APPROVAL_DECISION,
            f"approval {status.value}",
            {
                "approval_id": approval_id,
                "step_id": step_id,
                "decision": decision.decision,
                "status": status.value,
                "decided_by": decision.decided_by,
                "note": decision.note,
                "edited": decision.decision == "edit_args",
            },
            step_id=db_step_id,
        )
        async with session_scope() as session:
            refreshed = await session.get(Approval, approval_id)
            return refreshed  # type: ignore[return-value]

    async def _record_approval_decision(
        self, run_id: str, step: Any, outcome: Any, approval_id: str | None
    ) -> None:
        """Executor-side view: mark the step/step-row to match the decision."""
        if outcome.status is ApprovalStatus.TIMED_OUT:
            async with session_scope() as session:
                if approval_id:
                    row = await session.get(Approval, approval_id)
                    if row is not None and row.status == ApprovalStatus.PENDING.value:
                        row.status = ApprovalStatus.TIMED_OUT.value
                        row.decided_at = utcnow()
                        row.decided_by = "system"
                        row.decision_note = "auto-rejected: no human response before timeout"
            await self._emit(
                run_id,
                EventType.APPROVAL_DECISION,
                "approval timed out -> auto-rejected",
                {"step_id": step.id, "status": outcome.status.value, "auto": True},
            )
        if not outcome.granted:
            await self._set_status(run_id, RunStatus.RUNNING)

    # --- screenshots ----------------------------------------------------------
    async def _record_screenshots(
        self, run_id: str, plan_step_id: str, shots: list[str], state: dict[str, Any]
    ) -> None:
        """Persist new screenshot paths and mirror them into the run state."""
        if not shots:
            return
        db_step_id = await self._ensure_step(run_id, plan_step_id)
        known = state.setdefault("screenshot_paths", [])
        history = state.setdefault("screenshots", [])
        new_rows: list[tuple[str, int]] = []
        for src in shots:
            if src in known:
                continue
            known.append(src)
            new_rows.append((src, len(known) - 1))
            history.append({"src": src, "url": state.get("current_url", "")})
        if not new_rows:
            return
        async with session_scope() as session:
            for src, seq in new_rows:
                session.add(
                    Screenshot(
                        run_id=run_id,
                        step_id=db_step_id,
                        label=str(src).rsplit("/", 1)[-1],
                        path=str(src),
                        url=state.get("current_url") or None,
                        sequence=seq,
                    )
                )

    # --- finalisation ---------------------------------------------------------
    async def _persist_outcome(
        self,
        run_id: str,
        outcome: RunOutcome,
        state: dict[str, Any],
        started: float,
    ) -> None:
        async with session_scope() as session:
            run = await session.get(Run, run_id)
            if run is None:  # pragma: no cover
                return
            run.result = {
                **outcome.to_dict(),
                "final_answer": outcome.final_answer,
                "artifacts": state.get("final_artifacts", []),
                "report_markdown": state.get("report_markdown", ""),
            }
            run.status = outcome.status.value
            run.replan_count = outcome.replans
            run.steps_total = len(outcome.steps)
            run.steps_succeeded = outcome.succeeded_steps
            run.finished_at = utcnow()
            run.duration_ms = round((time.perf_counter() - started) * 1000, 1)
            if outcome.error:
                run.error = {
                    "code": outcome.error_code or "run_error",
                    "message": outcome.error,
                }

    async def _finalise(
        self,
        run_id: str,
        outcome: RunOutcome,
        state: dict[str, Any],
        started: float,
    ) -> None:
        """Emit the terminal event, close the SSE channel and write the report."""
        event = {
            RunStatus.COMPLETED: EventType.RUN_COMPLETED,
            RunStatus.FAILED: EventType.RUN_FAILED,
            RunStatus.CANCELLED: EventType.RUN_CANCELLED,
        }.get(outcome.status, EventType.RUN_COMPLETED)
        await self._emit(
            run_id, event, outcome.final_answer or outcome.error or "",
            {
                "status": outcome.status.value,
                "final_answer": outcome.final_answer,
                "steps_total": len(outcome.steps),
                "steps_succeeded": outcome.succeeded_steps,
                "steps_failed": outcome.failed_steps,
                "steps_skipped": outcome.skipped_steps,
                "step_accuracy": round(outcome.step_accuracy, 4),
                "replans": outcome.replans,
                "intervention": outcome.intervention,
                "error": outcome.error,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        with contextlib.suppress(Exception):
            from app.reports.generator import write_report_files

            await write_report_files(run_id)
        get_bus().close_channel(run_id)
        self.manager.forget(run_id)

    async def _finish_cancelled(
        self, run_id: str, started: float, *, reason: str = "cancelled by the user"
    ) -> None:
        async with session_scope() as session:
            run = await session.get(Run, run_id)
            if run is not None:
                run.status = RunStatus.CANCELLED.value
                run.finished_at = utcnow()
                run.duration_ms = round((time.perf_counter() - started) * 1000, 1)
                run.error = {"code": "run_cancelled", "message": reason}
            pending = await session.execute(
                select(Approval).where(
                    Approval.run_id == run_id,
                    Approval.status == ApprovalStatus.PENDING.value,
                )
            )
            for approval in pending.scalars().all():
                approval.status = ApprovalStatus.CANCELLED.value
                approval.decided_at = utcnow()
                approval.decided_by = "system"
                approval.decision_note = reason
        await self._emit(
            run_id, EventType.RUN_CANCELLED, reason,
            {"status": RunStatus.CANCELLED.value,
             "duration_ms": round((time.perf_counter() - started) * 1000, 1)},
        )
        get_bus().close_channel(run_id)
        self.manager.forget(run_id)

    async def _finish_failed(
        self, run_id: str, started: float, exc: TaskPilotError
    ) -> None:
        payload = (
            exc.to_dict()
            if isinstance(exc, TaskPilotError)
            else {"code": "internal_error", "message": str(exc), "retryable": True}
        )
        async with session_scope() as session:
            run = await session.get(Run, run_id)
            if run is not None:
                run.status = RunStatus.FAILED.value
                run.finished_at = utcnow()
                run.duration_ms = round((time.perf_counter() - started) * 1000, 1)
                run.error = payload
        await self._emit(
            run_id, EventType.RUN_FAILED, payload.get("message", "run failed"),
            {**payload, "duration_ms": round((time.perf_counter() - started) * 1000, 1)},
        )
        get_bus().close_channel(run_id)
        self.manager.forget(run_id)

    # --- cancellation ---------------------------------------------------------
    async def cancel_run(
        self, run_id: str, *, reason: str = "cancelled by the user"
    ) -> bool:
        run = await self.get_run(run_id)
        if run.status in {s.value for s in RunStatus.terminal()}:
            return False
        accepted = self.manager.cancel(run_id)
        if self.manager.is_running(run_id):
            return accepted  # the task's own handler writes the terminal row
        await self._finish_cancelled(run_id, time.perf_counter(), reason=reason)
        return True

    # --- plan review (plan-preview mode) --------------------------------------
    async def approve_plan(self, run_id: str) -> bool:
        """Release a run that was parked for plan review."""
        run = await self.get_run(run_id)
        if run.status != RunStatus.AWAITING_PLAN_APPROVAL.value:
            return False
        async with session_scope() as session:
            row = await session.get(Run, run_id)
            if row is not None:
                row.status = RunStatus.RUNNING.value
        await self._emit(run_id, EventType.PLAN_APPROVED, "plan approved", {})
        await self._resume_after_review(run_id)
        return True

    async def reject_plan(self, run_id: str, *, reason: str = "plan rejected") -> bool:
        run = await self.get_run(run_id)
        if run.status != RunStatus.AWAITING_PLAN_APPROVAL.value:
            return False
        async with session_scope() as session:
            row = await session.get(Run, run_id)
            if row is not None:
                row.status = RunStatus.CANCELLED.value
                row.finished_at = utcnow()
                row.error = {"code": "plan_rejected", "message": reason}
        await self._emit(run_id, EventType.PLAN_REJECTED, reason, {})
        get_bus().close_channel(run_id)
        return True

    async def _resume_after_review(self, run_id: str) -> None:
        """Re-enter execution for a run that was parked before executing."""
        async with session_scope() as session:
            run = await self._load_run(session, run_id)
            goal = run.goal
            plan_payload = run.plan or {}
        plan_result = _plan_result_from_payload(plan_payload)
        if plan_result is None:
            await self._finish_failed(
                run_id, time.perf_counter(), TaskPilotError("stored plan could not be parsed")
            )
            return
        task = asyncio.create_task(
            self._run_executor(run_id, goal, plan_result, {}, time.perf_counter()),
            name=f"taskpilot-run-{run_id[:8]}",
        )
        self.manager._tasks[run_id] = task
        task.add_done_callback(lambda _t: self.manager._tasks.pop(run_id, None))


# ---------------------------------------------------------------------------
# module helpers
# ---------------------------------------------------------------------------
async def _find_step(
    session: AsyncSession, run_id: str, plan_step_id: str
) -> Step | None:
    """Find a step row by its *plan* step id (the short ``s1``-style label).

    The plan id is what appears in events and on the approval card, so it is the
    identifier the API contract exposes; the UUID stays an internal detail.
    """
    result = await session.execute(select(Step).where(Step.run_id == run_id).order_by(Step.index))
    for step in result.scalars().all():
        if step.id == plan_step_id or _plan_step_id_of(step) == plan_step_id:
            return step
    return None


def _plan_step_id_of(step: Step) -> str:
    """Recover the plan's step id (``s1``…) for a persisted row."""
    args = getattr(step, "args", {}) or {}
    if isinstance(args, dict) and args.get("__plan_step_id"):
        return str(args["__plan_step_id"])
    return f"s{int(step.index) + 1}"


async def _latest_pending_approval(
    session: AsyncSession, run_id: str, step_db_id: str
) -> Approval | None:
    result = await session.execute(
        select(Approval)
        .where(
            Approval.run_id == run_id,
            Approval.step_id == step_db_id,
            Approval.status == ApprovalStatus.PENDING.value,
        )
        .order_by(Approval.requested_at.desc())
        .limit(1)
    )
    return result.scalars().first()


def _plan_result_from_payload(payload: dict[str, Any]) -> PlanResult | None:
    """Rebuild a :class:`PlanResult` (with fresh policy decisions) from stored JSON."""
    try:
        plan = Plan.model_validate(
            {k: v for k, v in payload.items() if k in Plan.model_fields}
        )
    except Exception:  # noqa: BLE001 - a malformed stored plan is not fatal
        return None
    from app.agent.policy import PolicyEngine

    registry = get_registry()
    engine = PolicyEngine()
    steps: list[Any] = []
    for index, step in enumerate(plan.steps):
        tool = registry.get(step.tool) if registry.has(step.tool) else None
        steps.append(
            PlannedStep(step=step, index=index, decision=engine.classify(step, tool), tool=tool)
        )
    return PlanResult(plan=plan, steps=steps)


def _title_from_prompt(prompt: str) -> str:
    first = prompt.strip().splitlines()[0] if prompt.strip() else "Untitled task"
    return (first[:80] + "…") if len(first) > 80 else first


def _trim(value: Any, limit: int = 8000) -> Any:
    """Bound what we store so a huge page cannot bloat a row."""
    text = str(value)
    return value if len(text) <= limit else text[:limit] + "…"


_orchestrator: Orchestrator | None = None


def get_orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator


def reset_orchestrator() -> None:
    """Test helper."""
    global _orchestrator
    _orchestrator = None


__all__ = [
    "Orchestrator",
    "RunManager",
    "get_manager",
    "get_orchestrator",
    "reset_manager",
    "reset_orchestrator",
]
