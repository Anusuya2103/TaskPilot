"""The approval gate, retries and idempotency.

The first half of this file is the most important test code in the repository:
it proves the executor **cannot** invoke a gated tool without a recorded human
decision, in every path — no response, explicit rejection, and cancellation.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.agent.approvals import ApprovalBroker
from app.agent.executor import Executor
from app.agent.planner import PlannedStep
from app.agent.policy import PolicyEngine
from app.core.models import ApprovalStatus, RiskLevel, StepStatus
from app.core.schemas import PlanStep, ToolResult
from app.tools.base import Tool, ToolContext


class RecordingTool(Tool):
    """Stands in for a dangerous action; records every execution."""

    name = "dangerous_action"
    description = "Performs an irreversible action"
    category = "browser"
    requires_approval_hint = True

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_times = 0

    @property
    def args_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"target": {"type": "string"}, "amount": {"type": "string"}},
            "required": ["target"],
        }

    async def _run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if self.fail_times > 0:
            self.fail_times -= 1
            return ToolResult(ok=False, summary="temporary failure", error="temporary failure")
        self.calls.append(args)
        return ToolResult(ok=True, summary=f"did it to {args.get('target')}")


@pytest.fixture
def danger(registry):
    tool = RecordingTool()
    registry.register(tool)
    return tool


@pytest.fixture
def gated(danger):
    """A plan with one step policy classifies as high risk."""
    engine = PolicyEngine()
    step = PlanStep(
        id="s1",
        goal="Place the order and pay the customer",
        tool="dangerous_action",
        args={"target": "checkout", "amount": "100"},
        risk="low",
    )
    decision = engine.classify(step, danger)
    assert decision.effective_risk is RiskLevel.HIGH
    return [PlannedStep(step=step, index=0, decision=decision, tool=danger)]


def _executor(registry, broker, **kwargs) -> Executor:
    return Executor(registry=registry, broker=broker, **kwargs)


# ---------------------------------------------------------------------------
# THE GATE
# ---------------------------------------------------------------------------
async def test_gated_tool_never_runs_without_approval(registry, danger, gated):
    """No human response at all -> the tool is never invoked."""
    broker = ApprovalBroker(poll_interval=0.05)
    executor = _executor(registry, broker)
    executor.settings.approval_timeout_seconds = 1

    outcome = await executor.run_plan(run_id="r1", steps=gated, goal="pay")

    assert danger.calls == [], "GATE BREACH: gated tool ran without approval"
    assert outcome.status.value == "failed"
    assert outcome.intervention == "rejected"
    assert outcome.steps[0].status == StepStatus.SKIPPED.value


async def test_explicit_rejection_blocks_the_tool(registry, danger, gated):
    broker = ApprovalBroker(poll_interval=0.05)

    async def reject() -> None:
        await asyncio.sleep(0.05)
        broker.decide(
            run_id="r2", step_id="s1", status=ApprovalStatus.REJECTED, decided_by="tester"
        )

    executor = _executor(registry, broker)
    executor.settings.approval_timeout_seconds = 5
    outcome = (
        await asyncio.gather(executor.run_plan(run_id="r2", steps=gated, goal="pay"), reject())
    )[0]

    assert danger.calls == [], "GATE BREACH: tool ran after an explicit rejection"
    assert outcome.steps[0].status == StepStatus.SKIPPED.value
    assert "declined" in (outcome.error or "").lower()


async def test_cancellation_blocks_the_tool(registry, danger, gated):
    broker = ApprovalBroker(poll_interval=0.05)

    async def cancel() -> None:
        await asyncio.sleep(0.05)
        broker.cancel_run("r3")

    executor = _executor(registry, broker)
    executor.settings.approval_timeout_seconds = 5
    outcome = (
        await asyncio.gather(executor.run_plan(run_id="r3", steps=gated, goal="pay"), cancel())
    )[0]

    assert danger.calls == [], "GATE BREACH: tool ran after cancellation"
    assert outcome.steps[0].status == StepStatus.SKIPPED.value


async def test_approval_lets_the_tool_run_exactly_once(registry, danger, gated):
    broker = ApprovalBroker(poll_interval=0.05)

    async def approve() -> None:
        await asyncio.sleep(0.05)
        broker.decide(
            run_id="r4",
            step_id="s1",
            status=ApprovalStatus.APPROVED,
            args={"target": "checkout", "amount": "100"},
        )

    executor = _executor(registry, broker)
    executor.settings.approval_timeout_seconds = 5
    outcome = (
        await asyncio.gather(executor.run_plan(run_id="r4", steps=gated, goal="pay"), approve())
    )[0]

    assert len(danger.calls) == 1
    assert outcome.status.value == "completed"
    assert outcome.steps[0].approval is not None
    assert outcome.steps[0].approval.granted is True


async def test_edited_args_are_what_gets_executed(registry, danger, gated):
    """An edit-approval must change the *effective* arguments, not just the record."""
    broker = ApprovalBroker(poll_interval=0.05)

    async def approve_with_edit() -> None:
        await asyncio.sleep(0.05)
        broker.decide(
            run_id="r5",
            step_id="s1",
            status=ApprovalStatus.EDITED,
            args={"target": "smaller-checkout", "amount": "10"},
        )

    executor = _executor(registry, broker)
    executor.settings.approval_timeout_seconds = 5
    await asyncio.gather(
        executor.run_plan(run_id="r5", steps=gated, goal="pay"), approve_with_edit()
    )

    assert danger.calls == [{"target": "smaller-checkout", "amount": "10"}]


async def test_low_risk_step_runs_without_any_approval(registry, danger, settings):
    """The gate must not be so eager that the agent stops doing anything."""
    broker = ApprovalBroker(poll_interval=0.05)
    outcome = await _executor(registry, broker, settings=settings).run_plan(
        run_id="r6", steps=_safe_plan(danger, settings, "s1", "Open the cart", "cart"), goal="look"
    )
    assert outcome.status.value == "completed"
    assert len(danger.calls) == 1, "a low-risk step should have run unattended"
    assert not broker.is_waiting("r6")


# ---------------------------------------------------------------------------
# retries
# ---------------------------------------------------------------------------
def _safe_plan(danger, settings, step_id: str, goal: str, target: str) -> list[PlannedStep]:
    engine = PolicyEngine()
    step = PlanStep(id=step_id, goal=goal, tool="dangerous_action", args={"target": target})
    return [
        PlannedStep(
            step=step,
            index=0,
            decision=engine.classify(step, danger, settings=settings),
            tool=danger,
        )
    ]


async def test_step_is_retried_then_succeeds(registry, danger, settings):
    danger.fail_times = 1  # fail once, succeed on the retry
    executor = _executor(registry, ApprovalBroker(poll_interval=0.05), settings=settings)
    outcome = await executor.run_plan(
        run_id="r7", steps=_safe_plan(danger, settings, "s1", "Open the cart", "cart"), goal="try"
    )

    assert outcome.status.value == "completed"
    assert outcome.steps[0].attempts == 2
    assert len(danger.calls) == 1  # only the successful attempt recorded a call


async def test_step_fails_after_exhausting_retries(registry, danger, settings):
    danger.fail_times = 99
    executor = _executor(registry, ApprovalBroker(poll_interval=0.05), settings=settings)
    outcome = await executor.run_plan(
        run_id="r8", steps=_safe_plan(danger, settings, "s1", "Open the cart", "cart"), goal="try"
    )

    assert outcome.status.value == "failed"
    assert outcome.steps[0].attempts == settings.max_step_attempts
    assert outcome.error_code == "step_failed"
