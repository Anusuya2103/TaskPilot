"""Policy engine: deterministic risk classification.

These tests are the safety contract. If any of them fails, the product's central
claim ("no risky action happens without a human") is false.
"""

from __future__ import annotations

import pytest

from app.agent.policy import PolicyEngine
from app.core.models import RiskLevel
from app.core.schemas import PlanStep
from app.tools import register_builtin_tools


@pytest.fixture
def engine() -> PolicyEngine:
    return PolicyEngine()


@pytest.fixture
def reg():
    return register_builtin_tools()


def step(**kwargs) -> PlanStep:
    defaults = {
        "id": "s1",
        "goal": "do a thing",
        "tool": "browser_open",
        "args": {},
        "expected_outcome": "",
        "risk": "low",
    }
    defaults.update(kwargs)
    return PlanStep(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# the escalation guarantee
# ---------------------------------------------------------------------------
def test_llm_understating_risk_is_still_gated(engine, reg):
    """The planner says 'low'; the rules say 'high'. High wins."""
    s = step(
        goal="Place the order and pay",
        tool="browser_click",
        args={"selector": "#place-order"},
        risk="low",
    )
    decision = engine.classify(s, reg.get("browser_click"))
    assert decision.declared_risk is RiskLevel.LOW
    assert decision.effective_risk is RiskLevel.HIGH
    assert decision.needs_approval is True
    assert {r.rule for r in decision.reasons} >= {"payment_terms", "submit_control"}


def test_planner_flagging_alone_gates_the_step(engine, reg):
    s = step(
        goal="Forward this to a colleague",
        tool="browser_click",
        args={"selector": "#send"},
        needs_approval=True,
    )
    decision = engine.classify(s, reg.get("browser_click"))
    assert decision.effective_risk is RiskLevel.HIGH
    assert decision.needs_approval is True


def test_planner_can_only_raise_never_lower(engine, reg):
    """max(rules, declared) — a cautious planner is always believed."""
    s = step(
        goal="Read the confirmation page", tool="browser_open", args={"url": "/cart"}, risk="high"
    )
    decision = engine.classify(s, reg.get("browser_open"))
    assert decision.effective_risk is RiskLevel.HIGH
    assert decision.needs_approval is True


# ---------------------------------------------------------------------------
# read-only tools: vocabulary must not cause a pause
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("goal", "tool", "args"),
    [
        ("Open the checkout page", "browser_open", {"url": "/checkout"}),
        ("Confirm the cart total", "browser_open", {"url": "/cart"}),
        ("Extract the order rows", "browser_extract", {"item_selector": "tr"}),
        ("Report the outcome to the user", "task_complete", {"summary": "placed the order"}),
    ],
)
def test_read_only_tools_never_gate(engine, reg, goal, tool, args):
    """Risk is a property of effect, not of scary words in the description."""
    s = step(goal=goal, tool=tool, args=args)
    decision = engine.classify(s, reg.get(tool))
    assert decision.needs_approval is False, decision.explanation()


def test_non_submitting_form_fill_caps_at_medium(engine, reg):
    s = step(
        goal="Fill in the payment details",
        tool="browser_fill_form",
        args={"fields": [{"selector": "#card_number", "value": "4242"}], "submit": False},
    )
    decision = engine.classify(s, reg.get("browser_fill_form"))
    assert decision.effective_risk is RiskLevel.MEDIUM
    assert decision.needs_approval is False  # medium follows the strict-mode toggle


def test_submitting_form_is_high_risk(engine, reg):
    s = step(
        goal="Fill and submit the form",
        tool="browser_fill_form",
        args={"fields": [], "submit": True},
    )
    decision = engine.classify(s, reg.get("browser_fill_form"))
    assert decision.effective_risk is RiskLevel.HIGH
    assert decision.needs_approval is True


# ---------------------------------------------------------------------------
# rule families
# ---------------------------------------------------------------------------
def test_http_post_requires_approval_in_strict_mode(engine, reg, settings):
    settings.auto_approve_low_risk = False
    s = step(
        goal="Update the shipping address",
        tool="http_request",
        args={"url": "https://api.example.com/profile", "method": "POST"},
    )
    decision = engine.classify(s, reg.get("http_request"), settings=settings)
    assert decision.effective_risk is RiskLevel.MEDIUM
    assert decision.needs_approval is True  # strict mode pauses on medium too


def test_http_delete_is_always_high(engine, reg):
    s = step(
        goal="Remove the record",
        tool="http_request",
        args={"url": "https://api.example.com/items/1", "method": "DELETE"},
    )
    decision = engine.classify(s, reg.get("http_request"))
    assert decision.effective_risk is RiskLevel.HIGH
    assert decision.needs_approval is True


def test_http_post_to_checkout_is_high(engine, reg):
    s = step(
        goal="Call the API",
        tool="http_request",
        args={"url": "https://shop.example.com/checkout", "method": "POST"},
    )
    decision = engine.classify(s, reg.get("http_request"))
    assert decision.effective_risk is RiskLevel.HIGH


def test_file_overwrite_gates_only_when_the_file_exists(engine, reg):
    args = {"title": "T", "markdown": "x", "filename": "report.md"}
    tool = reg.get("write_report")
    clean = engine.classify(
        step(goal="Write the report", tool="write_report", args=args), tool, existing_paths=set()
    )
    assert clean.effective_risk is RiskLevel.LOW

    overwriting = engine.classify(
        step(goal="Write the report", tool="write_report", args=args),
        tool,
        existing_paths={"report.md"},
    )
    assert overwriting.effective_risk is RiskLevel.HIGH
    assert "file_overwrite" in {r.rule for r in overwriting.reasons}


def test_benign_read_verbs_do_not_gate(engine, reg):
    s = step(
        goal="Read the order report and count the rows",
        tool="browser_extract",
        args={"item_selector": "tr"},
    )
    decision = engine.classify(s, reg.get("browser_extract"))
    assert decision.needs_approval is False


def test_llm_second_opinion_can_only_raise(engine, reg):
    s = step(goal="Update a setting", tool="browser_click", args={"selector": "#save"})
    base = engine.classify(s, reg.get("browser_click"))
    raised = engine.classify(s, reg.get("browser_click"), llm_risk="high")
    assert base.effective_risk is RiskLevel.MEDIUM
    assert raised.effective_risk is RiskLevel.HIGH
    assert raised.needs_approval is True


def test_explanations_are_human_readable(engine, reg):
    s = step(
        goal="Place the order and pay now", tool="browser_click", args={"selector": "#place-order"}
    )
    text = engine.classify(s, reg.get("browser_click")).explanation()
    assert "payment" in text.lower()
    assert len(text) > 20


def test_approval_required_helper_is_not_configurable():
    """The one invariant: high always pauses, whatever the settings say."""
    from app.core.config import Settings

    for auto in (True, False):
        settings = Settings(auto_approve_low_risk=auto)
        assert PolicyEngine.approval_required(RiskLevel.HIGH, settings) is True
        assert PolicyEngine.approval_required(RiskLevel.LOW, settings) is False
