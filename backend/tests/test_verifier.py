"""Verifier: the cheap deterministic layer that decides step success.

This module is deliberately biased against false negatives. Failing a step that
actually worked is the worst outcome for an agent run, so the checks are narrow
and a lot of the code exists to *avoid* failing a good step: "at least one" must
not fail on three, an explicitly-permitted empty result must not fail, and a
step that did not ask for a list must not be judged empty. None of that was
tested, so a well-meaning tightening could have quietly broken every run.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.verifier import Verifier
from app.core.schemas import PlanStep


def step(**kwargs: Any) -> PlanStep:
    data: dict[str, Any] = {
        "id": "s1",
        "goal": "do a thing",
        "tool": "browser_open",
        "args": {},
        "expected_outcome": "",
        "risk": "low",
    }
    data.update(kwargs)
    return PlanStep(**data)


def obs(**kwargs: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"summary": "fine"}
    data.update(kwargs)
    return data


@pytest.fixture
def verifier() -> Verifier:
    return Verifier()


# ---------------------------------------------------------------------------
# hard failures: these must always fail the step
# ---------------------------------------------------------------------------
def test_tool_failure_fails_the_step(verifier):
    v = verifier.check(step(), obs(error="selector not found"), tool_ok=False)
    assert v.decided is True
    assert v.success is False
    assert "selector not found" in v.reason


def test_tool_failure_without_detail_still_fails(verifier):
    v = verifier.check(step(), {}, tool_ok=False)
    assert v.success is False
    assert "unknown error" in v.reason


def test_http_error_status_fails_a_browser_step(verifier):
    v = verifier.check(step(tool="browser_open"), obs(status=500), tool_ok=True)
    assert v.success is False
    assert "500" in v.reason


def test_http_404_fails_a_browser_step(verifier):
    v = verifier.check(step(tool="browser_open"), obs(status=404), tool_ok=True)
    assert v.success is False


def test_http_error_status_ignored_for_non_browser_tools(verifier):
    """A 404 from an HTTP API tool is the tool's business, not the page's."""
    v = verifier.check(step(tool="http_get"), obs(status=404), tool_ok=True)
    assert v.decided is False


def test_ok_status_does_not_fail(verifier):
    v = verifier.check(step(tool="browser_open"), obs(status=200), tool_ok=True)
    assert v.success is True


def test_error_page_text_fails_the_step(verifier):
    v = verifier.check(step(), obs(summary="Internal Server Error"), tool_ok=True)
    assert v.success is False
    assert "error message" in v.reason


def test_404_not_found_page_fails(verifier):
    v = verifier.check(step(), obs(summary="404 Not Found"), tool_ok=True)
    assert v.success is False


# ---------------------------------------------------------------------------
# false-positive guards: ordinary content must NOT fail
# ---------------------------------------------------------------------------
def test_ordinary_page_does_not_look_like_an_error(verifier):
    v = verifier.check(
        step(), obs(summary="Welcome to the shop. All fields required."), tool_ok=True
    )
    assert v.decided is False
    assert v.success is True


def test_product_page_mentioning_error_policy_is_fine(verifier):
    text = "Returns accepted. Error handling policy: contact us."
    v = verifier.check(step(), obs(summary=text), tool_ok=True)
    assert v.success is True


def test_a_single_weak_marker_does_not_fail_the_step(verifier):
    """One weak word like 'required' is far too noisy to fail on."""
    v = verifier.check(step(), obs(summary="Password required"), tool_ok=True)
    assert v.decided is False


# ---------------------------------------------------------------------------
# expected values must be present
# ---------------------------------------------------------------------------
def test_missing_expected_price_fails_the_step(verifier):
    s = step(expected_outcome="the page shows a price of $19.99")
    v = verifier.check(s, obs(summary="Total: $24.99"), tool_ok=True)
    assert v.success is False
    assert "19.99" in v.reason


def test_present_expected_price_passes(verifier):
    s = step(expected_outcome="the page shows a price of $19.99")
    v = verifier.check(s, obs(summary="Only $19.99 today"), tool_ok=True)
    assert v.decided is False


def test_price_matching_ignores_trailing_zeros(verifier):
    s = step(expected_outcome="a price of $4.99")
    v = verifier.check(s, obs(summary="Now 4.9900"), tool_ok=True)
    assert v.decided is False


def test_price_matching_ignores_thousands_separators(verifier):
    s = step(expected_outcome="a price of $1,299.00")
    v = verifier.check(s, obs(summary="Price: 1299.00"), tool_ok=True)
    assert v.decided is False


def test_unparseable_money_expectation_is_not_enforced(verifier):
    """Never fail a step because we could not parse its own wording."""
    assert Verifier._money_close("not-a-number", "anything") is True


# ---------------------------------------------------------------------------
# counts: exact vs lower bound
# ---------------------------------------------------------------------------
def test_exact_count_mismatch_fails(verifier):
    s = step(goal="list results", expected_outcome="exactly three rows are shown")
    v = verifier.check(s, obs(items=[1, 2]), tool_ok=True)
    assert v.success is False
    assert "three" in v.reason


def test_exact_count_match_passes(verifier):
    s = step(goal="list results", expected_outcome="exactly three rows are shown")
    v = verifier.check(s, obs(items=[1, 2, 3]), tool_ok=True)
    assert v.decided is False


def test_lower_bound_satisfied_by_more_items(verifier):
    """ "at least one" must not fail because three came back."""
    s = step(goal="collect results", expected_outcome="at least one result")
    v = verifier.check(s, obs(items=[1, 2, 3]), tool_ok=True)
    assert v.decided is False


def test_lower_bound_shortfall_fails(verifier):
    s = step(goal="collect results", expected_outcome="at least three results")
    v = verifier.check(s, obs(items=[1]), tool_ok=True)
    assert v.success is False


def test_count_from_explicit_count_field(verifier):
    s = step(goal="collect results", expected_outcome="at least two results")
    v = verifier.check(s, obs(count=5), tool_ok=True)
    assert v.decided is False


def test_item_count_field_is_also_read(verifier):
    assert Verifier._actual_count({"item_count": 4}) == 4


# ---------------------------------------------------------------------------
# explicitly-permitted empty results
# ---------------------------------------------------------------------------
def test_allows_empty_detects_the_phrasings():
    assert Verifier._allows_empty("or an explicit 'No orders yet' row") is True
    assert Verifier._allows_empty("or no results") is True
    assert Verifier._allows_empty("showing zero rows if any") is True


def test_allows_empty_is_false_for_a_normal_expectation():
    assert Verifier._allows_empty("the page shows three products") is False


def test_permitted_empty_result_passes_the_step(verifier):
    # "collect" is a verb _expects_item recognises, so the empty-result branch
    # actually engages instead of being skipped as not-applicable.
    s = step(goal="collect the orders", expected_outcome="the orders table, or no results")
    v = verifier.check(s, obs(items=[], text="No rows yet"), tool_ok=True)
    assert v.decided is True
    assert v.success is True


def test_permitted_empty_on_a_non_list_step_is_simply_undecided(verifier):
    """A step that never asked for a list is not judged empty at all."""
    s = step(goal="open the orders page", expected_outcome="the orders page, or no results")
    v = verifier.check(s, obs(items=[], text="No rows yet"), tool_ok=True)
    assert v.decided is False
    assert v.success is True


def test_permitted_empty_suppresses_a_missing_price_failure(verifier):
    """An empty-but-allowed result must not also fail on a missing price."""
    s = step(goal="read totals", expected_outcome="a total of $19.99, or no results")
    v = verifier.check(s, obs(items=[], text="No rows yet"), tool_ok=True)
    assert v.success is True


def test_permitted_empty_does_not_mask_a_tool_error(verifier):
    s = step(goal="read totals", expected_outcome="a total, or no results")
    v = verifier.check(s, obs(error="connection reset"), tool_ok=False)
    assert v.success is False


# ---------------------------------------------------------------------------
# empty-result detection is deliberately narrow
# ---------------------------------------------------------------------------
def test_step_that_did_not_ask_for_items_is_never_empty(verifier):
    s = step(goal="open the checkout page", expected_outcome="the checkout page loads")
    v = verifier.check(s, obs(items=[], count=0), tool_ok=True)
    assert v.decided is False


def test_zero_count_alone_does_not_mean_empty(verifier):
    """count=0 counts scraped rows, not page emptiness."""
    s = step(goal="add to cart", expected_outcome="the item is in the cart")
    v = verifier.check(s, obs(count=0, items=[{"name": "cable"}]), tool_ok=True)
    assert v.decided is False


def test_explicit_empty_cart_text_is_detected(verifier):
    s = step(goal="add to cart", expected_outcome="the cart contains the item")
    assert Verifier._is_empty_result(s, {"text": "Your cart is empty"}) is True


def test_populated_cart_is_not_empty(verifier):
    s = step(goal="add to cart", expected_outcome="the cart contains the item")
    assert Verifier._is_empty_result(s, {"items": [{"name": "cable"}]}) is False


def test_expects_item_recognises_collect_verbs():
    assert Verifier._expects_item(step(goal="add to cart")) is True
    assert Verifier._expects_item(step(goal="collect the results")) is True
    assert Verifier._expects_item(step(goal="browse the shop")) is False


# ---------------------------------------------------------------------------
# observation text assembly
# ---------------------------------------------------------------------------
def test_observation_text_gathers_the_useful_fields():
    text = Verifier._observation_text(
        {"summary": "s", "text": "t", "url": "u", "items": [1, 2], "structured": {"k": "v"}}
    )
    assert "s" in text and "t" in text and "u" in text
    assert "k" in text


def test_observation_text_ignores_empty_and_non_string_fields():
    text = Verifier._observation_text({"summary": "", "text": None, "url": "u"})
    assert text == "u"


# ---------------------------------------------------------------------------
# verify(): the two-layer entry point
# ---------------------------------------------------------------------------
async def test_deterministic_verdict_short_circuits_the_llm(verifier):
    v = await verifier.verify(step(), obs(error="nope"), tool_ok=False, use_llm=True)
    assert v.success is False
    assert v.confidence > 0.8


async def test_undecidable_step_trusts_the_tool_with_low_confidence(verifier):
    v = await verifier.verify(step(), obs(summary="all good"), tool_ok=True, use_llm=False)
    assert v.success is True
    assert v.confidence < 0.5
    assert "trusting the tool result" in v.reason


async def test_undecidable_failure_is_reported_as_failure(verifier):
    v = await verifier.verify(step(), obs(summary="hm"), tool_ok=False, use_llm=False)
    assert v.success is False
    assert "tool reported failure" in v.reason


def test_actual_count_is_none_when_not_visible(verifier):
    assert Verifier._actual_count({"summary": "no numbers here"}) is None


def test_is_exact_count_reads_exactly_as_exact():
    assert Verifier._is_exact_count("exactly three rows") is True
    assert Verifier._is_exact_count("only two items") is True


def test_is_exact_count_reads_lower_bounds_as_permissive():
    assert Verifier._is_exact_count("at least one item") is False
    assert Verifier._is_exact_count("more than two results") is False
    assert Verifier._is_exact_count("a minimum of one row") is False


def test_unqualified_count_wording_defaults_to_permissive():
    """Ambiguous phrasing must not fail a good run."""
    assert Verifier._is_exact_count("three rows are shown") is False
