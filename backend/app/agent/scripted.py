"""The deterministic scripted brain.

TaskPilot's demo mode, test-suite and evaluation harness must be **hermetic**:
identical plans, identical timings, no API keys, no rate limits, no network
surprises on stage. :class:`ScriptedLLM` provides that by implementing the same
:class:`~app.agent.llm.LLMClient` interface and returning canned-but-realistic
plans for the known task shapes.

It is *not* a stub that fakes success — it writes real plans against the real
MockShop DOM, and everything downstream (tools, policy, approval gate, verifier,
events) is the production code path. When a goal is unrecognised it plans a
deliberate "open, observe, then decide" sequence rather than guessing.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.agent.llm import LLMClient, LLMMessage, LLMResponse
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.demo.canned import match_canned_task

logger = get_logger(__name__)


def _step(
    sid: str,
    goal: str,
    tool: str,
    args: dict[str, Any],
    expected: str = "",
    risk: str = "low",
    approval: bool = False,
) -> dict[str, Any]:
    return {
        "id": sid,
        "goal": goal,
        "tool": tool,
        "args": args,
        "expected_outcome": expected,
        "risk": risk,
        "needs_approval": approval,
        "rationale": "scripted demo brain (deterministic mode)",
    }


# ---------------------------------------------------------------------------
# plan templates
# ---------------------------------------------------------------------------
def _plan_buy_cheapest_cable() -> dict[str, Any]:
    return {
        "goal": "Find the cheapest USB-C cable on MockShop, add it to the cart, and stop "
        "before paying so a human can approve the purchase.",
        "success_criteria": [
            "the cheapest USB-C cable is identified by price",
            "exactly one USB-C cable is in the cart",
            "the checkout form is completed",
            "a human approved the order before it was placed",
        ],
        "estimated_manual_minutes": 3.5,
        "notes": "Search results are sorted cheapest-first, so the first card is the "
        "cheapest match.",
        "steps": [
            _step(
                "s1",
                "Open the MockShop search results for 'usb-c cable'",
                "browser_open",
                {"url": "/search?q=usb-c%20cable", "wait_for": ".product-card"},
                "the results page lists USB-C cable products with prices",
            ),
            _step(
                "s2",
                "Extract the name and price of every USB-C cable on the page",
                "browser_extract",
                {
                    "item_selector": ".product-card",
                    "fields": {"name": ".product-name", "price": ".price"},
                    "limit": 10,
                },
                "at least one item with a name and a price",
            ),
            _step(
                "s3",
                "Open the cheapest cable's product page (results are cheapest-first)",
                "browser_click",
                {"selector": '[data-testid="view-product"]', "index": 0},
                "a product detail page showing a name, price and an Add to cart button",
            ),
            _step(
                "s4",
                "Add the cable to the cart",
                "browser_click",
                {"selector": "#add-to-cart"},
                "the cart contains exactly one USB-C cable",
            ),
            _step(
                "s5",
                "Open the cart and confirm the contents and total",
                "browser_open",
                {"url": "/cart"},
                "the cart lists one USB-C cable and a total price",
            ),
            _step(
                "s6",
                "Open the checkout page",
                "browser_open",
                {"url": "/checkout"},
                "the checkout page shows the order total and a shipping form",
            ),
            _step(
                "s7",
                "Fill in the shipping and payment details (do not submit)",
                "browser_fill_form",
                {
                    "fields": [
                        {"selector": "#full_name", "value": "Anita Rao"},
                        {"selector": "#email", "value": "anita.rao@example.com"},
                        {"selector": "#address", "value": "42 Pilot Way, Bengaluru 560001"},
                        {"selector": "#card_number", "value": "4242 4242 4242 4242"},
                    ],
                    "submit": False,
                },
                "every checkout field holds the supplied value",
            ),
            _step(
                "s8",
                "Place the order and pay",
                "browser_click",
                {"selector": "#place-order"},
                "an order confirmation page showing an order id and the amount paid",
                risk="high",
                approval=True,
            ),
            _step(
                "s9",
                "Extract the order id and paid amount from the confirmation",
                "browser_extract",
                {"text_selector": '[data-testid="order-confirmation"]'},
                "the order id and total appear in the extracted text",
            ),
            _step(
                "s10",
                "Report the outcome",
                "task_complete",
                {
                    "summary": "Found the cheapest USB-C cable on MockShop (USB-C Cable 1m "
                    "(Basic) at $4.99), added one to the cart, completed the checkout "
                    "details and — after your approval — placed the order. The "
                    "confirmation page shows the order id and the amount charged.",
                    "artifacts": [],
                },
                "the user receives a complete final answer",
            ),
        ],
    }


def _plan_lab_report() -> dict[str, Any]:
    return {
        "goal": "Fill in the MockShop lab-report form with the supplied values and submit it "
        "once a human approves.",
        "success_criteria": [
            "all six form fields hold the supplied values",
            "a human approved the submission",
            "a confirmation page shows a report id",
        ],
        "estimated_manual_minutes": 2.0,
        "notes": "Submission is irreversible, so it is modelled as its own approval-gated step.",
        "steps": [
            _step(
                "s1",
                "Open the lab-report form",
                "browser_open",
                {"url": "/lab-report", "wait_for": "#patient_name"},
                "the lab-report form with the patient fields visible",
            ),
            _step(
                "s2",
                "Fill every field of the lab-report form (do not submit)",
                "browser_fill_form",
                {
                    "fields": [
                        {"selector": "#patient_name", "value": "Anita Rao"},
                        {"selector": "#patient_id", "value": "PT-88213"},
                        {"selector": "#test_type", "value": "Lipid Panel", "type": "select"},
                        {"selector": "#sample_date", "value": "2026-09-28"},
                        {"selector": "#priority", "value": "urgent", "type": "select"},
                        {"selector": "#notes", "value": "Fasting sample, 12h."},
                    ],
                    "submit": False,
                },
                "all six fields are populated with the supplied values",
            ),
            _step(
                "s3",
                "Submit the lab report",
                "browser_click",
                {"selector": "#submit-report"},
                "a confirmation showing a report id for Anita Rao's Lipid Panel",
                risk="high",
                approval=True,
            ),
            _step(
                "s4",
                "Extract the report confirmation details",
                "browser_extract",
                {"item_selector": "table tr", "limit": 8},
                "the confirmation page lists the patient, test and priority",
            ),
            _step(
                "s5",
                "Report the outcome",
                "task_complete",
                {
                    "summary": "Submitted the lab report for Anita Rao (patient PT-88213): "
                    "Lipid Panel, sample dated 2026-09-28, priority urgent, note "
                    "'Fasting sample, 12h.' The form was filed and a report id was "
                    "returned on the confirmation page.",
                    "artifacts": [],
                },
                "the user receives a complete final answer",
            ),
        ],
    }


def _plan_buy_named_cable() -> dict[str, Any]:
    return {
        "goal": "Add the USB-C Cable 2m Nylon Braided to the MockShop cart and place the order "
        "after human approval.",
        "success_criteria": ["the named cable is in the cart", "a human approved the order"],
        "estimated_manual_minutes": 4.0,
        "notes": "Targets product 2 directly for a second, distinct purchase path.",
        "steps": [
            _step(
                "s1",
                "Open the search results for 'usb-c cable'",
                "browser_open",
                {"url": "/search?q=usb-c%20cable", "wait_for": ".product-card"},
                "the results page lists USB-C cable products",
            ),
            _step(
                "s2",
                "Open the second result, the USB-C Cable 2m Nylon Braided",
                "browser_click",
                {"selector": '[data-testid="view-product"]', "index": 1},
                "the product page for the USB-C Cable 2m Nylon Braided",
            ),
            _step(
                "s3",
                "Add the braided cable to the cart",
                "browser_click",
                {"selector": "#add-to-cart"},
                "the cart contains the braided cable",
            ),
            _step(
                "s4",
                "Open the cart",
                "browser_open",
                {"url": "/cart"},
                "the cart lists the braided cable and a total",
            ),
            _step(
                "s5",
                "Open the checkout page",
                "browser_open",
                {"url": "/checkout"},
                "the checkout page shows the order total",
            ),
            _step(
                "s6",
                "Fill in shipping and payment details (do not submit)",
                "browser_fill_form",
                {
                    "fields": [
                        {"selector": "#full_name", "value": "Anita Rao"},
                        {"selector": "#email", "value": "anita.rao@example.com"},
                        {"selector": "#address", "value": "42 Pilot Way, Bengaluru 560001"},
                        {"selector": "#card_number", "value": "4242 4242 4242 4242"},
                    ],
                    "submit": False,
                },
                "all checkout fields hold the supplied values",
            ),
            _step(
                "s7",
                "Place the order and pay",
                "browser_click",
                {"selector": "#place-order"},
                "an order confirmation page with an order id",
                risk="high",
                approval=True,
            ),
            _step(
                "s8",
                "Report the outcome",
                "task_complete",
                {
                    "summary": "Added the USB-C Cable 2m Nylon Braided ($7.49) to the cart and "
                    "placed the order after your approval. The confirmation page "
                    "shows the order id.",
                    "artifacts": [],
                },
                "the user receives a complete final answer",
            ),
        ],
    }


def _plan_scrape_books(books_url: str) -> dict[str, Any]:
    return {
        "goal": f"Scrape the top 5 books from {books_url} (title and price) and write a "
        "Markdown summary report.",
        "success_criteria": [
            "five books are extracted with title and price",
            "a Markdown report exists in the run workspace",
        ],
        "estimated_manual_minutes": 12.0,
        "notes": "Read-only task: the approval gate should never trigger.",
        "steps": [
            _step(
                "s1",
                f"Open the {books_url} catalogue",
                "browser_open",
                {"url": books_url, "wait_for": ".product_pod"},
                "the book catalogue with product cards",
            ),
            _step(
                "s2",
                "Extract the title and price of the first five books",
                "browser_extract",
                {
                    "item_selector": ".product_pod",
                    "fields": {"title": "h3 a", "price": ".price_color"},
                    "limit": 5,
                },
                "five items each with a title and a price",
            ),
            _step(
                "s3",
                "Write the summary report to a Markdown file",
                "write_report",
                {
                    "title": "Top 5 books catalogue scrape",
                    "filename": "top-books.md",
                    "markdown": (
                        "## Source\n\n" + books_url + "\n\n"
                        "## Result\n\nFive books were collected with their titles and prices. "
                        "The raw structured extraction is reproduced below so every number can "
                        "be checked against the page.\n\n"
                        "```json\n{{observed.items}}\n```\n\n"
                        "## Method\n\nTaskPilot opened the catalogue, extracted each "
                        "`.product_pod` card (title + price) directly from the DOM and wrote "
                        "this file. No page content was ever treated as an instruction.\n"
                    ),
                },
                "a Markdown file containing the five books and their prices",
            ),
            _step(
                "s4",
                "Report the outcome",
                "task_complete",
                {
                    "summary": "Scraped the top 5 books from books.toscrape.com with their "
                    "titles and prices and wrote them to `top-books.md` in the run "
                    "workspace. Nothing was submitted or changed anywhere, so no "
                    "approval was required.",
                    "artifacts": ["top-books.md"],
                },
                "the user receives a complete final answer",
            ),
        ],
    }


def _plan_dashboard_read() -> dict[str, Any]:
    return {
        "goal": "Read the MockShop dashboard and report how many orders exist and the revenue.",
        "success_criteria": ["the order count and total are reported to the user"],
        "estimated_manual_minutes": 1.5,
        "notes": "Pure read path — proves the agent does not pause when nothing is at risk.",
        "steps": [
            _step(
                "s1",
                "Open the MockShop dashboard",
                "browser_open",
                {"url": "/dashboard", "wait_for": '[data-testid="orders-table"]'},
                "the orders and lab-report tables are visible",
            ),
            _step(
                "s2",
                "Extract every order row from the dashboard",
                "browser_extract",
                {
                    "item_selector": '[data-testid="order-row"]',
                    "fields": {"order_id": '[data-testid="order-row-id"]', "row": "td"},
                    "limit": 50,
                },
                "one record per order, or an explicit 'No orders yet' row",
            ),
            _step(
                "s3",
                "Report the outcome",
                "task_complete",
                {
                    "summary": "Read the MockShop dashboard: it currently lists the orders and "
                    "lab reports below. No approvals were needed because nothing was "
                    "modified.",
                    "artifacts": [],
                },
                "the user receives the dashboard contents",
            ),
        ],
    }


def _plan_lab_exercise() -> dict[str, Any]:
    return {
        "goal": "Fetch the lab-exercise endpoint, save the raw output, and write a short "
        "Markdown summary of what came back.",
        "success_criteria": [
            "the endpoint output is saved to a file",
            "a Markdown summary exists",
        ],
        "estimated_manual_minutes": 8.0,
        "notes": "Exercises the API + file tooling path (no browser, no approval).",
        "steps": [
            _step(
                "s1",
                "Fetch the lab-exercise endpoint",
                "http_get",
                {"url": "https://httpbin.org/json"},
                "a JSON response with status 200",
            ),
            _step(
                "s2",
                "Save the raw response to a file",
                "file_write",
                {"path": "lab-output.json", "content": "{{observed.text}}"},
                "a file lab-output.json exists in the workspace",
            ),
            _step(
                "s3",
                "Write a Markdown summary of the captured output",
                "write_report",
                {
                    "title": "Lab exercise — tool output summary",
                    "filename": "lab-exercise.md",
                    "markdown": (
                        "## What was run\n\n`GET https://httpbin.org/json`\n\n"
                        "## Captured output\n\n```json\n{{observed.text}}\n```\n\n"
                        "## Notes\n\nThe output above was fetched with the read-only HTTP tool "
                        "and written to `lab-output.json`. Nothing was submitted anywhere.\n"
                    ),
                },
                "a Markdown file summarising the tool output",
            ),
            _step(
                "s4",
                "Report the outcome",
                "task_complete",
                {
                    "summary": "Ran the lab exercise: fetched https://httpbin.org/json, saved the "
                    "raw response to `lab-output.json` and wrote a summary to "
                    "`lab-exercise.md`. Nothing was submitted, so no approval was "
                    "required.",
                    "artifacts": ["lab-output.json", "lab-exercise.md"],
                },
                "the user receives the summary and the artifact paths",
            ),
        ],
    }


def _plan_generic(goal: str, tools: list[str], base: str) -> dict[str, Any]:
    """Fallback for an unrecognised goal: observe first, never guess, then report."""
    use_browser = "browser_open" in tools
    return {
        "goal": goal,
        "success_criteria": ["the starting page was observed", "the user got a grounded answer"],
        "estimated_manual_minutes": 2.0,
        "notes": "Deterministic fallback: gather evidence before acting.",
        "steps": [
            _step(
                "s1",
                "Open the target site and observe its current state",
                "browser_open" if use_browser else "http_get",
                {"url": base},
                "the page loads and its contents are captured",
            ),
            _step(
                "s2",
                "Extract the readable content of the page",
                "browser_extract",
                {"limit": 30},
                "structured content is captured from the page",
            ),
            _step(
                "s3",
                "Report what was observed",
                "task_complete",
                {
                    "summary": "Observed the target site but could not complete the goal with "
                    "confidence. The captured content is attached to this run.",
                    "artifacts": [],
                },
                "the user receives an honest account of what was observed",
            ),
        ],
    }


def _plan_cart_inspect() -> dict[str, Any]:
    return {
        "goal": "Read the MockShop cart and report its contents and total.",
        "success_criteria": ["the cart contents and total are reported to the user"],
        "estimated_manual_minutes": 1.0,
        "notes": "Read-only: the agent must not ask for approval to look at a cart.",
        "steps": [
            _step(
                "s1",
                "Open the MockShop cart",
                "browser_open",
                {"url": "/cart", "wait_for": '[data-testid="cart-link"]'},
                "the cart page loads",
            ),
            _step(
                "s2",
                "Read the cart lines and the total",
                "browser_extract",
                {
                    "item_selector": '[data-testid="cart-row"]',
                    "fields": {
                        "name": '[data-testid="cart-item-name"]',
                        "qty": '[data-testid="cart-item-qty"]',
                        "price": '[data-testid="cart-item-subtotal"]',
                    },
                    "text_selector": '[data-testid="cart-total"]',
                    "limit": 20,
                },
                "the cart contents and the total are captured",
            ),
            _step(
                "s3",
                "Report the outcome",
                "task_complete",
                {
                    "summary": "Read the MockShop cart and reported the lines and total shown "
                    "in the extracted data. Nothing was modified, so no approval "
                    "was required.",
                    "artifacts": [],
                },
                "the user receives the cart contents",
            ),
        ],
    }


def _plan_browse() -> dict[str, Any]:
    return {
        "goal": "Open the MockShop catalogue and list the products that are in stock.",
        "success_criteria": ["the in-stock products are listed for the user"],
        "estimated_manual_minutes": 2.0,
        "notes": "Plain browsing — a zero-interruption path.",
        "steps": [
            _step(
                "s1",
                "Open the MockShop catalogue",
                "browser_open",
                {"url": "/", "wait_for": ".product-card"},
                "the product grid is visible",
            ),
            _step(
                "s2",
                "Extract every product with its name, price and stock",
                "browser_extract",
                {
                    "item_selector": ".product-card",
                    "fields": {"name": ".product-name", "price": ".price", "meta": ".meta"},
                    "limit": 20,
                },
                "one record per product with a name and a price",
            ),
            _step(
                "s3",
                "Report the outcome",
                "task_complete",
                {
                    "summary": "Browsed the MockShop catalogue and listed the products with "
                    "their prices as captured above. Nothing was changed, so no "
                    "approval was required.",
                    "artifacts": [],
                },
                "the user receives the catalogue listing",
            ),
        ],
    }


PLAN_BUILDERS: dict[str, Any] = {
    "mockshop_buy": _plan_buy_cheapest_cable,
    "lab_report": _plan_lab_report,
    "mockshop_buy_named": _plan_buy_named_cable,
    "scrape_books": lambda: _plan_scrape_books(get_settings().books_base_url),
    "dashboard_read": _plan_dashboard_read,
    "lab_exercise": _plan_lab_exercise,
    "cart_inspect": _plan_cart_inspect,
    "browse": _plan_browse,
}


def build_plan(goal: str, tools: list[str] | None = None) -> dict[str, Any]:
    """Route a goal to a canned plan, or to the honest fallback."""
    tools = tools or []
    canned = match_canned_task(goal)
    if canned is not None:
        builder = PLAN_BUILDERS.get(canned.intent)
        if builder is not None:
            return builder()
    return _plan_generic(goal, tools, get_settings().mockshop_base_url)


class ScriptedLLM(LLMClient):
    """A deterministic :class:`LLMClient` used by demo mode, tests and eval.

    It answers the three structured requests the agent makes — ``Plan``,
    ``VerificationVerdict`` and ``RiskAssessment`` — by pattern-matching the
    rendered prompt, then emitting a JSON document that validates against the
    real schema. Nothing here stubs out a tool result or an approval: the gate
    still has to classify, pause and record decisions, so the demo exercises the
    production path end to end.
    """

    provider = "scripted"

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings or get_settings())
        self.model = "taskpilot-scripted-v1"
        self.calls: list[str] = []

    async def _complete(
        self,
        messages: list[LLMMessage],
        *,
        system: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> LLMResponse:
        system_text = system or ""
        user_text = "\n".join(m.content for m in messages)
        blob = f"{system_text}\n{user_text}"
        self.calls.append(blob[:200])

        # `complete_structured` embeds the JSON Schema in the system prompt, whose
        # "title" field is the schema name. Match on that first, then fall back to
        # prompt-content heuristics.
        if '"title": "Plan"' in system_text:
            payload = build_plan(_extract_goal(user_text), _available_tools(user_text))
        elif '"title": "VerificationVerdict"' in system_text:
            payload = self._verification(user_text)
        elif '"title": "RiskAssessment"' in system_text:
            payload = self._risk(user_text)
        else:
            payload = build_plan(_extract_goal(user_text), [])

        text = json.dumps(payload)
        return LLMResponse(
            text=text,
            provider=self.provider,
            model=self.model,
            tokens_in=len(blob) // 4,
            tokens_out=len(text) // 4,
            stop_reason="end_turn",
        )

    # --- per-schema answers ---------------------------------------------------
    @staticmethod
    def _verification(user_text: str) -> dict[str, Any]:
        """Scripted verifier: trust the deterministic checks upstream of us.

        The executor only reaches the LLM layer when deterministic verification
        was inconclusive, so "no contradiction" is the right scripted answer.
        """
        ok = "TOOL REPORTED OK: true" in user_text
        return {
            "success": ok,
            "confidence": 0.9,
            "reason": (
                "Scripted mode: the deterministic checks found no contradiction with the "
                "expected outcome."
                if ok
                else "Scripted mode: the tool reported a failure."
            ),
            "missing": [],
        }

    @staticmethod
    def _risk(user_text: str) -> dict[str, Any]:
        """Scripted risk opinion: mirror the planner, then let policy raise it.

        The important property is that the scripted brain is *allowed to
        understate* risk, because :mod:`app.agent.policy` independently catches
        payment/submit keywords anyway. That is exactly the guarantee the eval
        harness measures.
        """
        match = re.search(r"STATED RISK BY PLANNER: (\w+)", user_text)
        declared = match.group(1) if match else "low"
        if declared not in {"low", "medium", "high"}:
            declared = "low"
        return {
            "risk": declared,
            "reasons": ["scripted risk opinion (policy engine remains authoritative)"],
            "irreversible": declared == "high",
        }


def _available_tools(user_text: str) -> list[str]:
    """Tool names as rendered by ``Tool.prompt_description()``."""
    return re.findall(r"^- (\w+) \(risk=", user_text, re.MULTILINE)


def _extract_goal(user_text: str) -> str:
    """Pull the task goal out of a rendered planner/replanner prompt."""
    for pattern in (
        r"USER GOAL:\s*\n(.+?)(?:\n\n|\Z)",
        r"ORIGINAL GOAL:\s*\n(.+?)(?:\n\n|\Z)",
        r"^Goal:\s*(.+)$",
    ):
        match = re.search(pattern, user_text, re.MULTILINE | re.DOTALL)
        if match:
            return match.group(1).strip()
    return user_text.strip()[:400]


__all__ = ["PLAN_BUILDERS", "ScriptedLLM", "build_plan"]
