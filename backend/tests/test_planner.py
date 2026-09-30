"""Planner: tool validation, plan capping, and human plan edits.

`finalise` is where a hallucinated tool name is caught and where every step gets
its policy verdict attached. Both are load-bearing for the approval gate, and
both were untested. The plan-preview edits matter just as much: a human removing
the "place the order" step must actually remove it.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from pydantic import ValidationError

from app.agent.planner import PlannedStep, Planner, PlanResult
from app.agent.policy import PolicyDecision, PolicyEngine
from app.core.errors import LLMError
from app.core.models import RiskLevel
from app.core.schemas import Plan, PlanStep
from app.tools import register_builtin_tools
from app.tools.registry import get_registry


def step(step_id: str = "s1", tool: str = "browser_open", **kwargs: Any) -> PlanStep:
    data: dict[str, Any] = {
        "id": step_id,
        "goal": "do a thing",
        "tool": tool,
        "args": {},
        "expected_outcome": "it works",
        "risk": "low",
    }
    data.update(kwargs)
    return PlanStep(**data)


def plan(*steps: PlanStep) -> Plan:
    return Plan(goal="do things", steps=list(steps))


@pytest.fixture
def reg():
    return register_builtin_tools()


@pytest.fixture
def planner(reg) -> Planner:
    from app.agent.scripted import ScriptedLLM

    return Planner(ScriptedLLM(), reg, None)


# ---------------------------------------------------------------------------
# finalise(): tool validation
# ---------------------------------------------------------------------------
def test_known_tools_are_kept(planner, reg):
    result = planner.finalise(plan(step("s1", "browser_open")), started=time.perf_counter())
    assert [s.step.tool for s in result.steps] == ["browser_open"]
    assert result.warnings == []


def test_close_typo_in_tool_name_is_repaired(planner, reg):
    """A one-character slip should self-correct, not kill the run."""
    result = planner.finalise(plan(step("s1", "browser_opne")), started=time.perf_counter())
    assert result.steps[0].step.tool == "browser_open"
    assert any("unknown tool" in w for w in result.warnings)


def test_unrecognisable_tool_is_dropped(planner, reg):
    # NB: "teleport" fuzzy-matches "write_report", so a nonsense name is needed
    # to actually exercise the drop branch.
    result = planner.finalise(
        plan(step("s1", "browser_open"), step("s2", "quantum_flux")),
        started=time.perf_counter(),
    )
    assert [s.step.id for s in result.steps] == ["s1"]
    assert any("dropped" in w for w in result.warnings)


def test_plan_with_no_usable_steps_raises_a_helpful_error(planner, reg):
    with pytest.raises(LLMError) as exc:
        planner.finalise(plan(step("s1", "quantum_flux")), started=time.perf_counter())
    assert "no usable steps" in str(exc.value)


def test_a_close_enough_typo_is_repaired_rather_than_dropped(planner, reg):
    """Even a nonsense-looking name is repaired if something is close enough."""
    result = planner.finalise(plan(step("s1", "teleport")), started=time.perf_counter())
    assert len(result.steps) == 1
    assert result.steps[0].step.tool == "write_report"


def test_plan_schema_rejects_an_empty_step_list():
    """The LLM schema itself forbids a zero-step plan, before finalise runs."""
    with pytest.raises(ValidationError):
        Plan(goal="do things", steps=[])


# ---------------------------------------------------------------------------
# finalise(): size cap
# ---------------------------------------------------------------------------
def test_plan_is_capped_at_max_steps(planner, reg):
    steps = [step(f"s{i}", "browser_open") for i in range(10)]
    result = planner.finalise(plan(*steps), started=time.perf_counter(), max_steps=3)
    assert len(result.steps) == 3
    assert any("truncated" in w for w in result.warnings)


def test_cap_uses_the_settings_default_when_not_given(planner, reg):
    steps = [step(f"s{i}", "browser_open") for i in range(planner.settings.max_steps + 5)]
    result = planner.finalise(plan(*steps), started=time.perf_counter())
    assert len(result.steps) == planner.settings.max_steps


# ---------------------------------------------------------------------------
# finalise(): policy verdicts
# ---------------------------------------------------------------------------
def test_every_step_gets_a_policy_verdict(planner, reg):
    result = planner.finalise(plan(step("s1", "browser_open")), started=time.perf_counter())
    assert result.steps[0].decision is not None
    assert result.steps[0].index == 0


def test_payment_step_is_marked_for_approval(planner, reg):
    result = planner.finalise(
        plan(step("s1", "browser_click", goal="Place the order and pay", risk="low")),
        started=time.perf_counter(),
    )
    assert result.requires_approval is True
    assert result.steps[0].decision.needs_approval is True


def test_benign_step_does_not_require_approval(planner, reg):
    result = planner.finalise(
        plan(step("s1", "browser_open", goal="Open the product page")),
        started=time.perf_counter(),
    )
    assert result.requires_approval is False


def test_plan_result_reports_highest_risk(planner, reg):
    result = planner.finalise(plan(step("s1", "browser_open")), started=time.perf_counter())
    assert isinstance(result.highest_risk, RiskLevel)
    assert result.to_dict()["highest_risk"] == result.highest_risk.value


def test_plan_result_serialises_steps_and_warnings(planner, reg):
    result = planner.finalise(plan(step("s1", "browser_open")), started=time.perf_counter())
    data = result.to_dict()
    assert data["steps"][0]["id"] == "s1"
    assert "risk_reasons" in data["steps"][0]
    assert "requires_approval" in data


def test_planned_step_serialises_status_and_error(planner, reg):
    result = planner.finalise(plan(step("s1", "browser_open")), started=time.perf_counter())
    data = result.steps[0].to_dict()
    assert data["status"] == "pending"
    assert data["error"] is None


# ---------------------------------------------------------------------------
# apply_edit(): the plan-preview path
# ---------------------------------------------------------------------------
def test_edit_can_remove_a_step(planner, reg):
    p = plan(step("s1"), step("s2"))
    applied = Planner.apply_edit(p, [{"id": "s2", "remove": True}], registry=reg)
    assert [s.id for s in p.steps] == ["s1"]
    assert any("removed" in a for a in applied)


def test_removing_the_purchase_step_removes_the_approval_requirement(planner, reg):
    """A human removing the risky step must actually clear the gate."""
    p = plan(
        step("s1", "browser_open"),
        step("s2", "browser_click", goal="Place the order and pay", risk="low"),
    )
    Planner.apply_edit(p, [{"id": "s2", "remove": True}], registry=reg)
    result = planner.finalise(p, started=time.perf_counter())
    assert result.requires_approval is False


def test_edit_can_change_args(planner, reg):
    p = plan(step("s1", "browser_click", args={"selector": "#a"}))
    Planner.apply_edit(p, [{"id": "s1", "args": {"selector": "#b"}}], registry=reg)
    assert p.steps[0].args["selector"] == "#b"


def test_arg_edit_merges_rather_than_replaces(planner, reg):
    p = plan(step("s1", "browser_click", args={"selector": "#a", "timeout": 5}))
    Planner.apply_edit(p, [{"id": "s1", "args": {"selector": "#b"}}], registry=reg)
    assert p.steps[0].args == {"selector": "#b", "timeout": 5}


def test_edit_can_change_the_goal(planner, reg):
    p = plan(step("s1"))
    Planner.apply_edit(p, [{"id": "s1", "goal": "new goal"}], registry=reg)
    assert p.steps[0].goal == "new goal"


def test_edit_can_swap_the_tool_when_it_exists(planner, reg):
    p = plan(step("s1", "browser_open"))
    Planner.apply_edit(p, [{"id": "s1", "tool": "browser_click"}], registry=reg)
    assert p.steps[0].tool == "browser_click"


def test_edit_cannot_swap_in_an_unknown_tool(planner, reg):
    p = plan(step("s1", "browser_open"))
    Planner.apply_edit(p, [{"id": "s1", "tool": "teleport"}], registry=reg)
    assert p.steps[0].tool == "browser_open"


def test_edit_for_unknown_step_is_reported_not_raised(planner, reg):
    p = plan(step("s1"))
    applied = Planner.apply_edit(p, [{"id": "nope", "goal": "x"}], registry=reg)
    assert any("unknown step" in a for a in applied)
    assert len(p.steps) == 1


def test_no_edits_is_a_no_op(planner, reg):
    p = plan(step("s1"))
    assert Planner.apply_edit(p, [], registry=reg) == []
    assert len(p.steps) == 1


def test_non_dict_args_edit_is_ignored(planner, reg):
    p = plan(step("s1", args={"a": 1}))
    Planner.apply_edit(p, [{"id": "s1", "args": "not a dict"}], registry=reg)
    assert p.steps[0].args == {"a": 1}


# ---------------------------------------------------------------------------
# misc dataclass behaviour
# ---------------------------------------------------------------------------
def test_plan_result_defaults_are_sane():
    empty = PlanResult(plan=plan(step()))
    assert empty.requires_approval is False
    assert empty.steps == []


def test_policy_engine_is_reachable_from_the_planner(planner):
    assert isinstance(planner.policy, PolicyEngine)


def test_registry_is_the_global_one_by_default(planner):
    assert planner.registry is get_registry()


def test_planned_step_defaults_are_pending():
    decision = PolicyDecision(declared_risk=RiskLevel.LOW, effective_risk=RiskLevel.LOW)
    planned = PlannedStep(step=step(), index=0, decision=decision)
    assert planned.status == "pending"
    assert planned.error is None
    assert planned.tool is None
