"""The four canned demo tasks (plus a couple of read-only warm-ups).

These are shown as chips on the landing page and used as the scripted brain's
first-class intents. Each entry declares the tool families it exercises so the UI
can badge them (Browser / MCP / API) and so tests can assert the plan shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MOCKSHOP_HINT = (
    "MockShop is a demo store at {base}. Product pages are /product/<id>; the cart "
    "is /cart; checkout is /checkout."
)


@dataclass(frozen=True)
class CannedTask:
    id: str
    title: str
    prompt: str
    needs_approval: bool
    tools: list[str] = field(default_factory=list)
    estimated_manual_minutes: float = 2.0
    category: str = "demo"
    intent: str = "generic"
    description: str = ""


CANNED_TASKS: list[CannedTask] = [
    CannedTask(
        id="mockshop-buy-cable",
        title="Buy the cheapest USB-C cable",
        prompt=(
            "Find the cheapest USB-C cable on MockShop, add it to the cart, and buy it. "
            "Stop and ask me before you pay."
        ),
        needs_approval=True,
        tools=["browser_open", "browser_extract", "browser_click", "browser_fill_form"],
        estimated_manual_minutes=3.5,
        intent="mockshop_buy",
        description="Browser automation + approval gate on an irreversible payment.",
    ),
    CannedTask(
        id="mockshop-lab-report",
        title="Submit a lab report",
        prompt=(
            "Fill the lab-report form on MockShop with these values and submit it: "
            "patient name 'Anita Rao', patient ID 'PT-88213', test type 'Lipid Panel', "
            "sample date '2026-09-28', priority 'urgent', notes 'Fasting sample, 12h.' "
            "Stop and ask me before you submit."
        ),
        needs_approval=True,
        tools=["browser_open", "browser_fill_form"],
        estimated_manual_minutes=2.0,
        intent="lab_report",
        description="Form filling + approval gate on submit.",
    ),
    CannedTask(
        id="scrape-books",
        title="Scrape the top 5 books + write a report",
        prompt=(
            "Scrape the top 5 books from books.toscrape.com (title and price, cheapest first) "
            "and write a summary report to a file. No approval needed."
        ),
        needs_approval=False,
        tools=["browser_open", "browser_extract", "write_report", "task_complete"],
        estimated_manual_minutes=12.0,
        intent="scrape_books",
        description="Read-only scraping, no approval — shows the zero-friction path.",
    ),
    CannedTask(
        id="mcp-lab-exercise",
        title="Run a tool, capture output, write it up",
        prompt=(
            "Run the lab exercise: fetch https://httpbin.org/json, save the raw output to a "
            "file with the file tools, then write a short Markdown summary of what it returned."
        ),
        needs_approval=False,
        tools=["http_get", "file_write", "report", "task_complete"],
        estimated_manual_minutes=8.0,
        category="lab",
        intent="lab_exercise",
        description="API + MCP/files tooling — no browser, no approval.",
    ),
    CannedTask(
        id="mockshop-read-dashboard",
        title="Read the orders dashboard",
        prompt=(
            "Open the MockShop dashboard and tell me how many orders have been placed and "
            "what the total revenue is."
        ),
        needs_approval=False,
        tools=["browser_open", "browser_extract", "task_complete"],
        estimated_manual_minutes=1.5,
        intent="dashboard_read",
        description="Pure read: proves the agent does not pause when nothing is at risk.",
    ),
    CannedTask(
        id="mockshop-double-checkout",
        title="Two cables, two orders (advanced)",
        prompt=(
            "On MockShop, add the USB-C Cable 2m Nylon Braided to the cart and place the order, "
            "then note the order id."
        ),
        needs_approval=True,
        tools=["browser_open", "browser_click", "browser_fill_form", "browser_extract"],
        estimated_manual_minutes=4.0,
        intent="mockshop_buy_named",
        description="Second payment path — used by the eval harness to vary the target.",
    ),
]

_BY_ID = {t.id: t for t in CANNED_TASKS}


def get_canned_task(task_id: str) -> CannedTask | None:
    return _BY_ID.get(task_id)


def match_canned_task(prompt: str) -> CannedTask | None:
    """Cheap keyword router used by the scripted brain and the UI."""
    text = (prompt or "").lower()
    if not text.strip():
        return None
    if ("lab" in text and "report" in text) or "lab-report" in text:
        return _BY_ID["mockshop-lab-report"]
    if "usb-c" in text or ("usb" in text and "cable" in text):
        if "cheapest" in text or "cheap" in text:
            return _BY_ID["mockshop-buy-cable"]
        return _BY_ID["mockshop-double-checkout"]
    if "book" in text and ("scrape" in text or "top 5" in text or "top five" in text):
        return _BY_ID["scrape-books"]
    if "dashboard" in text or "how many orders" in text:
        return _BY_ID["mockshop-read-dashboard"]
    if "httpbin" in text or "lab exercise" in text or "capture the output" in text:
        return _BY_ID["mcp-lab-exercise"]
    return None


__all__ = ["CANNED_TASKS", "CannedTask", "get_canned_task", "match_canned_task"]
