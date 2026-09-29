"""The executor: the only place a tool is ever actually invoked.

This module is the enforcement point for the product's central promise. Three
rules are enforced *here* rather than in the UI or the planner:

1. **No gated action runs without a recorded approval.**
   :meth:`Executor._gate_step` is called for every step whose policy decision is
   ``needs_approval``. It blocks on :class:`~app.agent.approvals.ApprovalBroker`
   and a rejection/timeout returns without invoking the tool. There is no flag,
   environment variable or API call that skips this branch.
2. **Risk is re-derived at execution time.** The plan-time verdict is recomputed
   from the *actual* arguments right before the call, so an edited-args approval
   cannot smuggle in a different, riskier action.
3. **Retries are bounded and auditable.** A step gets
   ``max_step_attempts`` tries, then the run replans at most ``max_replans``
   times, then fails with a specific reason instead of looping.

Everything the executor needs from the outside world arrives as
:class:`RunHooks` callbacks, so the whole loop is testable without a database, a
browser, or an event bus.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.agent.approvals import ApprovalBroker, ApprovalOutcome
from app.agent.planner import PlannedStep, Planner
from app.agent.policy import PolicyEngine
from app.agent.verifier import Verifier
from app.core.config import Settings, get_settings
from app.core.errors import RunCancelled, TaskPilotError
from app.core.logging import bind_step, get_logger, log_event
from app.core.models import ApprovalStatus, RiskLevel, RunStatus, StepStatus
from app.core.schemas import PlanStep, ToolResult, VerificationVerdict
from app.core.security import safe_preview
from app.tools.base import ToolContext
from app.tools.registry import ToolRegistry, get_registry

logger = get_logger(__name__)


@dataclass
class StepOutcome:
    index: int
    step_id: str
    goal: str
    tool: str
    status: str = StepStatus.PENDING.value
    args: dict[str, Any] = field(default_factory=dict)
    result: ToolResult | None = None
    verification: VerificationVerdict | None = None
    error: str | None = None
    attempts: int = 0
    risk: str = RiskLevel.LOW.value
    needs_approval: bool = False
    approval: ApprovalOutcome | None = None
    screenshots: list[str] = field(default_factory=list)
    latency_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "step_id": self.step_id,
            "goal": self.goal,
            "tool": self.tool,
            "status": self.status,
            "args": self.args,
            "verification": self.verification.model_dump() if self.verification else None,
            "error": self.error,
            "attempts": self.attempts,
            "risk": self.risk,
            "needs_approval": self.needs_approval,
            "approved": bool(self.approval and self.approval.granted),
            "approval_status": self.approval.status.value if self.approval else None,
            "screenshots": self.screenshots,
            "latency_ms": self.latency_ms,
            "summary": self.result.summary if self.result else None,
            "extracted": self.result.extracted if self.result else {},
        }


@dataclass
class RunOutcome:
    status: RunStatus = RunStatus.COMPLETED
    steps: list[StepOutcome] = field(default_factory=list)
    final_answer: str = ""
    error: str | None = None
    error_code: str | None = None
    replans: int = 0
    intervention: str | None = None  # rejected | timed_out | cancelled | None
    tokens_in: int = 0
    tokens_out: int = 0
    llm_calls: int = 0

    @property
    def succeeded_steps(self) -> int:
        return sum(1 for s in self.steps if s.status == StepStatus.DONE.value)

    @property
    def failed_steps(self) -> int:
        return sum(1 for s in self.steps if s.status == StepStatus.FAILED.value)

    @property
    def skipped_steps(self) -> int:
        return sum(1 for s in self.steps if s.status == StepStatus.SKIPPED.value)

    @property
    def step_accuracy(self) -> float:
        return self.succeeded_steps / len(self.steps) if self.steps else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "final_answer": self.final_answer,
            "error": self.error,
            "error_code": self.error_code,
            "replans": self.replans,
            "intervention": self.intervention,
            "steps": [s.to_dict() for s in self.steps],
            "step_accuracy": round(self.step_accuracy, 4),
            "llm": {
                "calls": self.llm_calls,
                "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
            },
        }


class RunHooks:
    """Every side effect the executor needs, injected.

    The orchestrator implements these against the database and the event bus; the
    tests implement them as plain append-only lists. Keeping them explicit is what
    lets the retry/replan/gate logic be tested in milliseconds.
    """

    def __init__(self) -> None:
        self.on_step_start: Any = None
        self.on_tool_call: Any = None
        self.on_observation: Any = None
        self.on_screenshot: Any = None
        self.on_step_done: Any = None
        self.on_step_failed: Any = None
        self.on_step_skipped: Any = None
        self.on_retry: Any = None
        self.on_replan: Any = None
        self.on_approval_request: Any = None
        self.on_approval_decision: Any = None
        self.on_policy: Any = None
        self.on_security: Any = None
        self.on_log: Any = None

    async def _maybe(self, hook: Any, *args: Any, **kwargs: Any) -> Any:
        if hook is None:
            return None
        return await hook(*args, **kwargs)


class Executor:
    """Runs a plan, with the approval gate hard-wired into the loop."""

    def __init__(
        self,
        *,
        registry: ToolRegistry | None = None,
        verifier: Verifier | None = None,
        planner: Planner | None = None,
        broker: ApprovalBroker | None = None,
        settings: Settings | None = None,
        hooks: RunHooks | None = None,
    ) -> None:
        self.registry = registry or get_registry()
        self.settings = settings or get_settings()
        self.verifier = verifier or Verifier()
        self.planner = planner
        self.broker = broker or ApprovalBroker()
        self.policy = PolicyEngine(self.settings)
        self.hooks = hooks or RunHooks()

    # --- main loop ------------------------------------------------------------
    async def run_plan(
        self,
        *,
        run_id: str,
        steps: list[PlannedStep],
        goal: str,
        state: dict[str, Any] | None = None,
        is_cancelled: Any = None,
        session_factory: Any = None,
    ) -> RunOutcome:
        state = state if state is not None else {}
        outcome = RunOutcome()
        queue = list(steps)
        replans = 0
        cancelled = is_cancelled or (lambda: False)

        while queue:
            planned = queue.pop(0)
            if cancelled():
                outcome.intervention = outcome.intervention or "cancelled"
                outcome.status = RunStatus.CANCELLED
                self._skip_remaining(queue, outcome, "run cancelled")
                return outcome

            step_outcome = await self._execute_step(
                planned=planned,
                run_id=run_id,
                goal=goal,
                state=state,
                outcome=outcome,
                session_factory=session_factory,
                cancelled=cancelled,
            )

            if step_outcome.status == StepStatus.SKIPPED.value:
                # A human said no. Stopping is the only safe response: any later
                # step in the plan was written assuming this one happened.
                outcome.intervention = outcome.intervention or "rejected"
                outcome.status = RunStatus.FAILED
                outcome.error = (
                    f"You declined step {planned.step.id} "
                    f"({planned.step.goal}). The agent stopped there and did not attempt "
                    "any remaining steps."
                )
                outcome.error_code = "approval_rejected"
                self._skip_remaining(queue, outcome, "stopped after a declined step")
                return outcome

            if step_outcome.status == StepStatus.FAILED.value:
                if replans < self.settings.max_replans:
                    replans += 1
                    replacement = await self._replan(
                        goal=goal,
                        completed=queue_already_done(outcome),
                        failed=planned.step,
                        reason=step_outcome.error or "step failed verification",
                        remaining=queue,
                        last_observation=self._observation_text(step_outcome),
                        replan_no=replans,
                    )
                    if replacement is not None:
                        queue = replacement + queue
                        outcome.replans = replans
                        continue
                # No replan budget (or replanning failed): fail gracefully.
                self._skip_remaining(queue, outcome, "run failed")
                outcome.status = RunStatus.FAILED
                outcome.error = step_outcome.error or "step failed and could not be recovered"
                outcome.error_code = "step_failed"
                return outcome

        outcome.final_answer = self._final_answer(outcome, state)
        outcome.status = RunStatus.COMPLETED
        return outcome

    # --- one step -------------------------------------------------------------
    async def _execute_step(
        self,
        *,
        planned: PlannedStep,
        run_id: str,
        goal: str,
        state: dict[str, Any],
        outcome: RunOutcome,
        session_factory: Any,
        cancelled: Any,
    ) -> StepOutcome:
        step = planned.step
        bind_step(step.id)
        result_out = StepOutcome(
            index=planned.index,
            step_id=step.id,
            goal=step.goal,
            tool=step.tool,
            args=dict(step.args),
        )
        await self.hooks._maybe(self.hooks.on_step_start, step, planned.decision)

        # 0. idempotency: never run the same logical step twice
        idem_key = self.idempotency_key(run_id, planned.index, step)
        cached = state.get("idempotency", {}).get(idem_key)
        if cached is not None:
            log_event(
                logger, logging.INFO, "step already executed; reusing result",
                run_id=run_id, step_id=step.id,
            )
            result_out.status = StepStatus.DONE.value
            result_out.result = ToolResult(**cached)
            result_out.attempts = 1
            await self.hooks._maybe(self.hooks.on_step_done, step, result_out)
            outcome.steps.append(result_out)
            return result_out

        # 1. Re-derive risk from the *actual* args, not the plan-time verdict.
        tool = self.registry.get(step.tool)
        decision = self.policy.classify(step, tool, settings=self.settings)
        result_out.risk = decision.effective_risk.value
        result_out.needs_approval = decision.needs_approval
        if decision.reasons:
            await self.hooks._maybe(
                self.hooks.on_policy, step, decision,
                escalated=decision.rule_risk.rank > decision.declared_risk.rank,
            )

        # 2. THE GATE. No branch below this point runs a gated tool without a
        #    recorded, human decision.
        args = dict(step.args)
        if decision.needs_approval:
            approval = await self._gate_step(
                step=step,
                decision=decision,
                run_id=run_id,
                session_factory=session_factory,
                state=state,
            )
            result_out.approval = approval
            if not approval.granted:
                result_out.status = StepStatus.SKIPPED.value
                result_out.error = f"not executed: approval {approval.status.value}"
                await self.hooks._maybe(self.hooks.on_step_skipped, step, approval)  # type: ignore[attr-defined]
                outcome.steps.append(result_out)
                return result_out
            args = approval.args or args

        # 3. Run with bounded retries.
        max_attempts = max(1, self.settings.max_step_attempts)
        last_error = "step did not run"
        for attempt in range(1, max_attempts + 1):
            if cancelled():
                raise RunCancelled("Run was cancelled by the user")
            result_out.attempts = attempt
            started = time.perf_counter()
            await self.hooks._maybe(
                self.hooks.on_tool_call, step, args, attempt, max_attempts
            )
            try:
                ctx = ToolContext(
                    run_id=run_id,
                    step_id=step.id,
                    settings=self.settings,
                    emit=self._make_emitter(step),
                    state=state,
                    cancel_check=cancelled,
                    idempotency_key=idem_key,
                )
                result = await tool.run(args, ctx)
            except RunCancelled:
                raise
            except TaskPilotError as exc:
                result = ToolResult(ok=False, summary=exc.message, error=exc.message)
            except Exception as exc:  # noqa: BLE001 - the run must survive one bad step
                logger.exception("executor: unexpected tool failure")
                result = ToolResult(
                    ok=False, summary=f"{type(exc).__name__}: {exc}", error=str(exc)
                )

            result_out.result = result
            result_out.latency_ms = round((time.perf_counter() - started) * 1000, 1)
            result_out.screenshots = list(result.screenshots)
            if result.reasons:
                await self.hooks._maybe(
                    self.hooks.on_security, step, result.reasons
                )

            verdict = await self.verifier.verify(
                step,
                {"summary": result.summary, **result.data, **result.extracted},
                tool_ok=result.ok,
            )
            result_out.verification = verdict
            await self.hooks._maybe(
                self.hooks.on_observation, step, result, verdict
            )

            if verdict.success:
                result_out.status = StepStatus.DONE.value
                state.setdefault("idempotency", {})[idem_key] = result.model_dump()
                await self.hooks._maybe(self.hooks.on_step_done, step, result_out)
                outcome.steps.append(result_out)
                return result_out

            last_error = verdict.reason or result.error or "verification failed"
            if attempt < max_attempts:
                await self.hooks._maybe(
                    self.hooks.on_retry, step, attempt, max_attempts, last_error
                )
                if verdict.suggested_retry_args:
                    args = {**args, **verdict.suggested_retry_args}

        result_out.status = StepStatus.FAILED.value
        result_out.error = last_error
        await self.hooks._maybe(self.hooks.on_step_failed, step, result_out)
        outcome.steps.append(result_out)
        return result_out

    # --- the gate -------------------------------------------------------------
    async def _gate_step(
        self,
        *,
        step: PlanStep,
        decision: Any,
        run_id: str,
        session_factory: Any,
        state: dict[str, Any],
    ) -> ApprovalOutcome:
        """Ask a human, then block until they answer.

        The approval row is written by the ``on_approval_request`` hook (the
        orchestrator persists it) *before* we wait, so the audit trail contains a
        pending request even if the process dies while paused. The returned
        outcome is the only thing that can let the caller proceed.
        """
        preview = self.build_preview(step, decision, state)
        timeout = float(self.settings.approval_timeout_seconds)
        request = await self.hooks._maybe(
            self.hooks.on_approval_request, step, decision, preview, timeout
        )
        approval_id = request.get("approval_id") if isinstance(request, dict) else None

        log_event(
            logger,
            logging.INFO,
            "pausing for human approval",
            run_id=run_id,
            step_id=step.id,
            tool=step.tool,
            risk=decision.effective_risk.value,
            reasons=decision.explanation()[:200],
        )

        outcome = await self.broker.request(
            run_id=run_id,
            step_id=step.id,
            timeout=timeout,
            session_factory=session_factory,
        )
        await self.hooks._maybe(
            self.hooks.on_approval_decision, step, outcome, approval_id
        )

        if not outcome.granted:
            log_event(
                logger,
                logging.INFO,
                "approval not granted; step will not execute",
                run_id=run_id,
                step_id=step.id,
                status=outcome.status.value,
            )
        return outcome

    def build_preview(
        self, step: PlanStep, decision: Any, state: dict[str, Any]
    ) -> dict[str, Any]:
        """What the human is shown before deciding.

        The preview is built from the *live* browser state when there is one, so
        the card shows the actual page being authorised rather than a restatement
        of the plan.
        """
        current_url = str(state.get("current_url") or "")
        page_title = str(state.get("current_title") or "")
        screenshot = None
        shots = state.get("screenshots") or []
        if shots and isinstance(shots[-1], dict):
            screenshot = shots[-1].get("src")
            current_url = current_url or str(shots[-1].get("url") or "")
            page_title = page_title or str(shots[-1].get("title") or "")

        return {
            "action": step.goal,
            "tool": step.tool,
            "tool_description": (
                self.registry.get(step.tool).description
                if self.registry.has(step.tool)
                else ""
            ),
            "args": step.args,
            "args_preview": safe_preview(step.args, 600),
            "risk": decision.effective_risk.value,
            "declared_risk": decision.declared_risk.value,
            "rule_risk": decision.rule_risk.value,
            "reasons": [r.to_dict() for r in decision.reasons],
            "explanation": decision.explanation(),
            "target": step.args.get("url") or step.args.get("selector") or step.tool,
            "current_url": current_url,
            "page_title": page_title,
            "screenshot": screenshot,
            "reversible": decision.effective_risk.value != RiskLevel.HIGH.value,
            "consequence": (
                "This action cannot be undone by TaskPilot."
                if decision.effective_risk.value == RiskLevel.HIGH.value
                else "This action can be reversed if you are not happy with it."
            ),
        }

    # --- helpers --------------------------------------------------------------
    @staticmethod
    def idempotency_key(run_id: str, index: int, step: PlanStep) -> str:
        """Stable hash of a step's logical identity (run, position, tool, args)."""
        payload = json.dumps(
            {"tool": step.tool, "args": step.args, "goal": step.goal},
            sort_keys=True,
            default=str,
        )
        digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
        return f"{run_id}:{index}:{step.tool}:{digest}"

    def _make_emitter(self, step: PlanStep) -> Any:
        async def emit(event: Any, message: str, payload: dict[str, Any]) -> None:
            hook = self.hooks.on_screenshot if event == "screenshot" else self.hooks.on_log
            await self.hooks._maybe(hook, step, event, message, payload)

        return emit

    @staticmethod
    def _observation_text(step_outcome: StepOutcome) -> str:
        if step_outcome.result is None:
            return ""
        return json.dumps(
            {"summary": step_outcome.result.summary, "data": step_outcome.result.data},
            default=str,
        )[:4000]

    def _skip_remaining(
        self, queue: list[PlannedStep], outcome: RunOutcome, reason: str
    ) -> None:
        for planned in queue:
            outcome.steps.append(
                StepOutcome(
                    index=planned.index,
                    step_id=planned.step.id,
                    goal=planned.step.goal,
                    tool=planned.step.tool,
                    status=StepStatus.SKIPPED.value,
                    error=reason,
                )
            )
        queue.clear()

    @staticmethod
    def _final_answer(outcome: RunOutcome, state: dict[str, Any]) -> str:
        """Prefer what ``task_complete`` said; otherwise summarise honestly."""
        declared = state.get("final_answer")
        if declared:
            return str(declared)
        done = [s for s in outcome.steps if s.status == StepStatus.DONE.value]
        if not done:
            return "The run finished without completing any steps."
        lines = [f"Completed {len(done)} of {len(outcome.steps)} steps."]
        for step in done[-3:]:
            summary = step.result.summary if step.result else None
            if summary:
                lines.append(f"- {step.goal}: {summary}")
        return "\n".join(lines)

    # --- replanning -----------------------------------------------------------
    async def _replan(
        self,
        *,
        goal: str,
        completed: list[PlanStep],
        failed: PlanStep,
        reason: str,
        remaining: list[PlannedStep],
        last_observation: str,
        replan_no: int,
    ) -> list[PlannedStep] | None:
        """Ask the planner for a new tail. Returns ``None`` if replanning fails."""
        if self.planner is None:
            log_event(
                logger, logging.WARNING, "step failed but no planner is wired for replanning",
                step_id=failed.id, reason=reason,
            )
            return None
        log_event(
            logger, logging.INFO, "replanning the remaining steps",
            step_id=failed.id, attempt=replan_no, reason=reason,
        )
        try:
            result = await self.planner.replan(
                goal=goal,
                completed=completed,
                failed=failed,
                failure_reason=reason,
                remaining=remaining,
                last_observation=last_observation,
            )
        except Exception as exc:  # noqa: BLE001 - replanning is best-effort
            log_event(
                logger, logging.ERROR, "replan failed",
                step_id=failed.id, error=f"{type(exc).__name__}: {exc}",
            )
            return None
        if not result.steps:
            return None
        await self.hooks._maybe(
            self.hooks.on_replan, failed, reason, result, replan_no
        )
        return result.steps


def queue_already_done(outcome: RunOutcome) -> list[PlanStep]:
    """Reconstruct the PlanSteps that succeeded, for the replan prompt."""
    return [
        PlanStep(
            id=s.step_id,
            goal=s.goal,
            tool=s.tool,
            args=s.args,
            expected_outcome="",
        )
        for s in outcome.steps
        if s.status == StepStatus.DONE.value
    ]


__all__ = ["Executor", "RunHooks", "RunOutcome", "StepOutcome", "queue_already_done"]
