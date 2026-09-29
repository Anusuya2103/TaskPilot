"""End-to-end: a real agent run against MockShop with a real browser.

Nothing is stubbed except the LLM (the deterministic scripted brain). The run
uses Chromium, the real policy engine, the real approval gate, the real event log
and the real report generator — so this is the test that would catch a regression
in the *product*, not just in a unit.

The assertion that matters most is at the bottom: an order exists in MockShop
**only because a human approved it**, and the audit trail proves both halves.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.models import ApprovalStatus, RunStatus, StepStatus
from app.core.schemas import ApprovalDecision, TaskCreate

pytestmark = pytest.mark.e2e

TERMINAL = {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value}


async def _wait_for_approval(orchestrator: Any, run_id: str, timeout: float = 90.0) -> str | None:
    """Wait until the run parks on a pending approval; return the plan step id."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        run = await orchestrator.get_run(run_id)
        for approval in run.approvals:
            if approval.status == ApprovalStatus.PENDING.value:
                return _plan_step_id(run, approval.step_id)
        if run.status in TERMINAL:
            return None
        await asyncio.sleep(0.2)
    return None


def _plan_step_id(run: Any, db_step_id: str) -> str:
    for step in run.steps:
        if step.id == db_step_id:
            return str((step.args or {}).get("__plan_step_id") or f"s{step.index + 1}")
    return db_step_id


async def _wait_for_terminal(orchestrator: Any, run_id: str, timeout: float = 120.0) -> Any:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        run = await orchestrator.get_run(run_id)
        if run.status in TERMINAL:
            return run
        await asyncio.sleep(0.25)
    raise AssertionError(f"run {run_id} did not reach a terminal state in {timeout}s")


def _store():
    from app.demo.mockshop_runner import load_mockshop_module

    return load_mockshop_module().store


async def test_buy_cheapest_cable_pauses_then_completes(mockshop_url):
    """The headline demo: search, add to cart, PAUSE at payment, approve, order."""
    from app.agent.orchestrator import Orchestrator

    orchestrator = Orchestrator()
    run = await orchestrator.create_task_run(
        TaskCreate(
            prompt="Find the cheapest USB-C cable on MockShop, add to cart, and buy it",
            auto_start=True,
        )
    )
    assert await orchestrator.start_run(run.id)

    # --- the agent must pause and ask before paying ------------------------
    gated_step = await _wait_for_approval(orchestrator, run.id)
    assert gated_step is not None, "the agent never requested approval for the payment"

    paused = await orchestrator.get_run(run.id)
    assert paused.status == RunStatus.AWAITING_APPROVAL.value

    store = _store()
    assert store.orders == [], "an order was placed BEFORE approval"
    assert store.cart, "the cable should be in the cart before the gate"

    # --- approve, and let the run finish -----------------------------------
    approval = await orchestrator.decide_approval(
        paused.id, gated_step, ApprovalDecision(decision="approve", decided_by="e2e")
    )
    assert approval.status == ApprovalStatus.APPROVED.value
    assert approval.decided_by == "e2e"

    final = await _wait_for_terminal(orchestrator, run.id)
    assert final.status == RunStatus.COMPLETED.value, final.error

    # --- the order exists, and only because we said yes ---------------------
    assert store.orders, "the order was not placed after approval"
    assert store.orders[0]["total"] == pytest.approx(4.99)
    assert store.orders[0]["items"][0]["name"] == "USB-C Cable 1m (Basic)"
    assert store.cart == [], "the cart should be empty after a completed order"

    # --- the audit trail is complete and honest -----------------------------
    done = [s for s in final.steps if s.status == StepStatus.DONE.value]
    gated = [s for s in final.steps if s.needs_approval]
    assert len(gated) == 1, f"expected exactly one gated step, got {len(gated)}"
    assert gated[0].approved_at is not None, "the gated step ran with no recorded approval"
    assert len(done) >= 8
    assert "cheapest USB-C cable" in (final.result or {}).get("final_answer", "")
    assert final.screenshots, "no screenshots were captured"


async def test_rejecting_the_payment_stops_the_run_without_ordering(mockshop_url):
    """A 'no' must leave the world untouched and the run honestly failed."""
    from app.agent.orchestrator import Orchestrator

    orchestrator = Orchestrator()
    run = await orchestrator.create_task_run(
        TaskCreate(prompt="Find the cheapest USB-C cable on MockShop, add to cart, and buy it")
    )
    await orchestrator.start_run(run.id)

    gated_step = await _wait_for_approval(orchestrator, run.id)
    assert gated_step is not None

    await orchestrator.decide_approval(
        run.id, gated_step,
        ApprovalDecision(decision="reject", decided_by="e2e", note="too expensive"),
    )
    final = await _wait_for_terminal(orchestrator, run.id)

    assert final.status == RunStatus.FAILED.value
    assert (final.error or {}).get("code") == "approval_rejected"
    assert "declined" in (final.error or {}).get("message", "").lower()

    store = _store()
    assert store.orders == [], "an order was placed despite an explicit rejection"
    assert store.cart, "the cart should still hold the item after a rejection"
    skipped = [s for s in final.steps if s.status == StepStatus.SKIPPED.value]
    assert skipped, "expected the remaining steps to be recorded as skipped"


async def test_read_only_task_never_pauses(mockshop_url):
    """The zero-interruption path: reading the dashboard needs no human."""
    from app.agent.orchestrator import Orchestrator

    # Seed an order so the dashboard is not empty — this also exercises reading
    # a store that has already been changed by an earlier run.
    from app.demo.mockshop_runner import load_mockshop_module

    store = load_mockshop_module().store
    store.reset()
    store.add_to_cart(1, 1)
    store.place_order(
        {"full_name": "Seed", "email": "s@example.com", "address": "1 St", "card_last4": "4242"}
    )

    orchestrator = Orchestrator()
    run = await orchestrator.create_task_run(
        TaskCreate(prompt="Open the MockShop dashboard and tell me how many orders exist")
    )
    await orchestrator.start_run(run.id)
    final = await _wait_for_terminal(orchestrator, run.id)

    assert final.status == RunStatus.COMPLETED.value, final.error
    assert final.approvals_count == 0, "a read-only task must not ask for approval"
    assert not [s for s in final.steps if s.needs_approval]


async def test_read_only_task_succeeds_on_an_empty_store(mockshop_url):
    """Zero orders is a valid dashboard state and must not be reported as a failure."""
    from app.agent.orchestrator import Orchestrator
    from app.demo.mockshop_runner import load_mockshop_module

    load_mockshop_module().store.reset()
    orchestrator = Orchestrator()
    run = await orchestrator.create_task_run(
        TaskCreate(prompt="Open the MockShop dashboard and tell me how many orders exist")
    )
    await orchestrator.start_run(run.id)
    final = await _wait_for_terminal(orchestrator, run.id)

    assert final.status == RunStatus.COMPLETED.value, final.error
    assert final.approvals_count == 0


async def test_lab_report_submission_pauses_before_submit(mockshop_url):
    """Demo task (b): filling a form is fine; submitting is gated."""
    from app.agent.orchestrator import Orchestrator

    orchestrator = Orchestrator()
    run = await orchestrator.create_task_run(
        TaskCreate(
            prompt=(
                "Fill the lab-report form on MockShop with these values and submit it: "
                "patient name 'Anita Rao', patient ID 'PT-88213', test type 'Lipid Panel', "
                "sample date '2026-09-28', priority 'urgent', notes 'Fasting sample, 12h.' "
                "Stop and ask me before you submit."
            )
        )
    )
    await orchestrator.start_run(run.id)

    gated_step = await _wait_for_approval(orchestrator, run.id)
    assert gated_step is not None
    store = _store()
    assert store.lab_reports == [], "the report was submitted BEFORE approval"

    await orchestrator.decide_approval(
        run.id, gated_step, ApprovalDecision(decision="approve", decided_by="e2e")
    )
    final = await _wait_for_terminal(orchestrator, run.id)

    assert final.status == RunStatus.COMPLETED.value, final.error
    assert len(store.lab_reports) == 1
    report = store.lab_reports[0]
    assert report["patient_name"] == "Anita Rao"
    assert report["test_type"] == "Lipid Panel"
    assert report["priority"] == "urgent"
