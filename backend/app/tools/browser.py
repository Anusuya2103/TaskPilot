"""Browser automation tools (Playwright, async Chromium).

Design commitments that matter for the demo and the audit trail:

* **One isolated browser context per run.** Cookies/localStorage never leak
  between runs, and a crashed run cannot poison the next one.
* **A screenshot after every action.** The UI's filmstrip and the report are
  built from these; they are also how a reviewer verifies an action happened.
* **Allowlist enforced here, not in the UI.** A plan that tries to leave the
  allowlist fails at the tool boundary.
* **Page content is data.** Everything extracted is passed through
  :func:`sanitize_untrusted` before it can reach a prompt.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urljoin

from app.core.errors import ToolError
from app.core.logging import get_logger
from app.core.models import RiskLevel
from app.core.schemas import ToolResult
from app.core.security import check_url_allowed, sanitize_untrusted
from app.tools.base import Tool, ToolContext

logger = get_logger(__name__)

MAX_TEXT_CHARS = 8000
MAX_ITEMS = 60


class BrowserSession:
    """Thin, crash-tolerant wrapper around a Playwright page.

    The Playwright objects are typed as ``Any`` because the async API returns
    dynamic proxies whose types do not survive a narrow annotation cleanly; they
    are held in locals that are guarded by ``None`` checks instead.
    """

    def __init__(self, run_id: str, headless: bool = True, timeout_ms: int = 20000) -> None:
        self.run_id = run_id
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._lock = asyncio.Lock()
        self.console: list[str] = []

    # --- lifecycle ------------------------------------------------------------
    async def ensure_started(self) -> Any:
        async with self._lock:
            if self._page is not None and not self._page.is_closed():
                return self._page
            from playwright.async_api import async_playwright

            if self._playwright is None:
                self._playwright = await async_playwright().start()
            if self._browser is None or not self._browser.is_connected():
                self._browser = await self._playwright.chromium.launch(
                    headless=self.headless,
                    args=[
                        "--disable-dev-shm-usage",
                        "--no-sandbox",
                        "--disable-gpu",
                        "--disable-background-networking",
                    ],
                )
            # Isolated context per run: no shared cache, no shared cookies.
            self._context = await self._browser.new_context(
                viewport={"width": 1366, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 TaskPilot/1.0"
                ),
                ignore_https_errors=True,
            )
            self._context.set_default_timeout(self.timeout_ms)
            self._page = await self._context.new_page()
            self._page.on("console", self._on_console)
            self._page.on("pageerror", self._on_pageerror)
            logger.info("browser context ready", extra={"extra_fields": {"run_id": self.run_id}})
            return self._page

    def _on_console(self, msg: Any) -> None:  # pragma: no cover - event hook
        try:
            if msg.type in {"error", "warning"}:
                self.console.append(f"{msg.type}: {msg.text}"[:400])
                del self.console[:-40]
        except Exception:
            pass

    def _on_pageerror(self, err: Any) -> None:  # pragma: no cover - event hook
        self.console.append(f"pageerror: {err}"[:400])
        del self.console[:-40]

    async def close(self) -> None:
        async with self._lock:
            for attr in ("_context", "_browser"):
                obj = getattr(self, attr, None)
                if obj is not None:
                    try:
                        await obj.close()
                    except Exception:  # pragma: no cover - best effort
                        pass
                    setattr(self, attr, None)
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:  # pragma: no cover
                    pass
                self._playwright = None
            self._page = None

    @property
    def started(self) -> bool:
        return self._page is not None and not self._page.is_closed()

    @property
    def current_url(self) -> str:
        try:
            return self._page.url if self._page else ""
        except Exception:  # pragma: no cover
            return ""


class BrowserManager:
    """Owns one :class:`BrowserSession` per run id."""

    def __init__(self, *, max_idle_seconds: float = 600.0) -> None:
        self._sessions: dict[str, BrowserSession] = {}
        self._last_used: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self.max_idle_seconds = max_idle_seconds

    async def get(self, run_id: str, settings: Any) -> BrowserSession:
        async with self._lock:
            session = self._sessions.get(run_id)
            if session is None:
                session = BrowserSession(
                    run_id=run_id,
                    headless=settings.browser_headless,
                    timeout_ms=settings.browser_timeout_ms,
                )
                self._sessions[run_id] = session
            self._last_used[run_id] = asyncio.get_event_loop().time()
            return session

    async def release(self, run_id: str) -> None:
        async with self._lock:
            session = self._sessions.pop(run_id, None)
            self._last_used.pop(run_id, None)
        if session is not None:
            await session.close()

    async def close_all(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._last_used.clear()
        await asyncio.gather(*(s.close() for s in sessions), return_exceptions=True)

    @property
    def active_run_ids(self) -> list[str]:
        return list(self._sessions)


_manager: BrowserManager | None = None


def get_browser_manager() -> BrowserManager:
    global _manager
    if _manager is None:
        _manager = BrowserManager()
    return _manager


def reset_browser_manager() -> None:
    """Test helper."""
    global _manager
    _manager = None


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------
async def _safe_screenshot(
    ctx: ToolContext, page: Any, label: str, *, full_page: bool = True
) -> str | None:
    """Capture a PNG and return its API-relative URL, or None if it fails."""
    try:
        ctx.screenshots_dir.mkdir(parents=True, exist_ok=True)
        seq = len(ctx.state.setdefault("screenshot_seq", []))
        ctx.state["screenshot_seq"].append(label)
        filename = f"{ctx.run_id[:8]}_{seq:03d}_{_slug(label)}.png"
        path = ctx.screenshots_dir / filename
        await page.screenshot(path=str(path), full_page=full_page)
        rel = f"/media/screenshots/{filename}"
        ctx.state.setdefault("screenshots", []).append(
            {
                "path": str(path),
                "src": rel,
                "label": label,
                "url": page.url,
                "title": await _safe_title(page),
            }
        )
        await ctx.notify("screenshot", f"screenshot: {label}", src=rel, label=label, url=page.url)
        return rel
    except Exception as exc:  # pragma: no cover - depends on browser
        logger.warning("screenshot failed: %s", exc)
        return None


def _slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit] or "step"


async def _safe_title(page: Any) -> str:
    try:
        return (await page.title())[:300]
    except Exception:  # pragma: no cover
        return ""


async def _page_summary(page: Any) -> dict[str, Any]:
    """Compact, structured view of the page for the verifier and the report."""
    try:
        text = await page.inner_text("body")
    except Exception:  # pragma: no cover
        text = ""
    sanitized = sanitize_untrusted(text, max_chars=MAX_TEXT_CHARS)
    try:
        links = await page.eval_on_selector_all(
            "a[href]",
            "els => els.slice(0, 40).map(e => ({text: (e.innerText||'').trim().slice(0,80),"
            " href: e.href}))",
        )
    except Exception:  # pragma: no cover
        links = []
    return {
        "url": page.url,
        "title": await _safe_title(page),
        "text": sanitized.text,
        "text_excerpt": sanitized.text[:1200],
        "security_flags": sanitized.flags,
        "links": links[:MAX_ITEMS],
    }


def _resolve_url(target: str, base_url: str | None, ctx: ToolContext) -> str:
    """Turn relative hrefs into absolute URLs and enforce the allowlist."""
    candidate = (target or "").strip()
    if not candidate:
        raise ToolError("A URL or path is required")
    if candidate.startswith(("http://", "https://")):
        url = candidate
    elif candidate.startswith("//"):
        url = f"https:{candidate}"
    else:
        base = base_url or ctx.settings.mockshop_base_url
        url = urljoin(base.rstrip("/") + "/", candidate.lstrip("/"))
    check_url_allowed(url, ctx.settings)
    return url


async def _resolve_selector(page: Any, selector: str, text: str | None = None) -> str:
    """Accept CSS, Playwright text selectors, or a plain human phrase.

    The planner is asked for CSS, but LLMs occasionally emit ``text=`` or raw
    visible text. Rather than fail the step we translate, which keeps the run
    resilient (and is exactly the "retry with repair" spirit).
    """
    selector = (selector or "").strip()
    if not selector and text:
        selector = f"text={text}"
    if not selector:
        raise ToolError("A selector (or text) is required to target an element")
    candidates = [selector]
    if selector.startswith("text="):
        candidates = [selector, f"button:has-text('{selector[5:]}')"]
    elif not any(ch in selector for ch in "#.[ >:["):
        candidates = [
            f"text={selector}",
            f"button:has-text('{selector}')",
            f"a:has-text('{selector}')",
        ]
    for cand in candidates:
        try:
            if await page.locator(cand).count() > 0:
                return cand
        except Exception:  # pragma: no cover - invalid selector
            continue
    raise ToolError(f"No element matched {' | '.join(candidates)!r} on {page.url}")


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------
class _BrowserToolBase(Tool):
    """Shared plumbing: acquire this run's isolated session, guard cancellation."""

    category = "browser"

    async def session(self, ctx: ToolContext) -> BrowserSession:
        ctx.raise_if_cancelled()
        session = await get_browser_manager().get(ctx.run_id, ctx.settings)
        await session.ensure_started()
        return session


class BrowserOpenTool(_BrowserToolBase):
    name = "browser_open"
    description = (
        "Open a URL in the agent's browser and capture the page. Use relative "
        "paths like '/product/3' for the MockShop demo store."
    )
    read_only = True
    risk_level = RiskLevel.LOW

    @property
    def args_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Absolute URL or path e.g. '/cart'"},
                "wait_for": {
                    "type": "string",
                    "description": "Optional CSS selector to wait for before returning",
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        }

    @property
    def examples(self) -> list[dict[str, Any]]:
        return [{"url": "/", "wait_for": "#search"}]

    async def _run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = await self.session(ctx)
        page = await session.ensure_started()
        url = _resolve_url(str(args["url"]), str(ctx.state.get("base_url") or "") or None, ctx)
        response = await page.goto(url, wait_until="domcontentloaded")
        status = response.status if response else None
        wait_for = args.get("wait_for")
        if wait_for:
            try:
                await page.wait_for_selector(
                    str(wait_for), timeout=min(8000, ctx.settings.browser_timeout_ms)
                )
            except Exception:
                logger.debug("wait_for %s timed out (continuing)", wait_for)
        await page.wait_for_timeout(120)
        shot = await _safe_screenshot(ctx, page, f"open {url}")
        summary = await _page_summary(page)
        summary["status"] = status
        ctx.state["base_url"] = (
            f"{url.split('://')[0]}://{url.split('://')[1].split('/')[0]}" if "://" in url else ""
        )
        ok = status is None or status < 400
        return ToolResult(
            ok=ok,
            summary=f"Opened {url} (HTTP {status}) — {summary['title']}",
            data={**summary, "status": status, "screenshots": [shot] if shot else []},
            extracted={"url": url, "title": summary["title"]},
            screenshots=[shot] if shot else [],
            url=url,
        )


class BrowserClickTool(_BrowserToolBase):
    name = "browser_click"
    description = (
        "Click an element (button, link, checkbox). Prefer a CSS selector; visible "
        "text also works. Clicking a submit/pay/send control is classified as high "
        "risk by the policy engine and will pause for human approval."
    )
    risk_level = RiskLevel.LOW
    requires_approval_hint = True

    @property
    def args_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector, e.g. '#add-to-cart'"},
                "text": {"type": "string", "description": "Visible text fallback"},
                "index": {
                    "type": "integer",
                    "description": "Which match to click (0-based)",
                    "default": 0,
                },
            },
            "required": ["selector"],
            "additionalProperties": False,
        }

    @property
    def examples(self) -> list[dict[str, Any]]:
        return [{"selector": "#add-to-cart"}]

    async def _run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = await self.session(ctx)
        page = await session.ensure_started()
        target = await _resolve_selector(page, str(args["selector"]), args.get("text"))
        index = int(args.get("index") or 0)
        before_url = page.url
        locator = page.locator(target).nth(index)
        await locator.scroll_into_view_if_needed()
        await locator.click(timeout=min(10000, ctx.settings.browser_timeout_ms))
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:  # pragma: no cover - SPA navigations
            pass
        await page.wait_for_timeout(200)
        label = f"click {target}"
        shot = await _safe_screenshot(ctx, page, label)
        summary = await _page_summary(page)
        navigated = summary["url"] != before_url
        return ToolResult(
            ok=True,
            summary=f"Clicked {target} — now on {summary['url']}",
            data={**summary, "navigated": navigated, "screenshots": [shot] if shot else []},
            extracted={"clicked": target, "url": summary["url"], "title": summary["title"]},
            screenshots=[shot] if shot else [],
            url=summary["url"],
        )


class BrowserTypeTool(_BrowserToolBase):
    name = "browser_type"
    description = (
        "Type text into an input/textarea (clears the field first). Set submit=true "
        "only when the field's own behaviour is the action; that is high risk."
    )

    def keyword_risk_ceiling(self, args: dict[str, Any]) -> RiskLevel:
        # Typing alone changes nothing on the server; pressing Enter can submit.
        return RiskLevel.HIGH if args.get("submit") else RiskLevel.MEDIUM

    @property
    def args_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector for the field"},
                "text": {"type": "string", "description": "Text to enter"},
                "submit": {
                    "type": "boolean",
                    "default": False,
                    "description": "Press Enter after typing (submits the form)",
                },
            },
            "required": ["selector", "text"],
            "additionalProperties": False,
        }

    @property
    def examples(self) -> list[dict[str, Any]]:
        return [{"selector": "#search", "text": "usb-c cable"}]

    async def _run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = await self.session(ctx)
        page = await session.ensure_started()
        target = await _resolve_selector(page, str(args["selector"]))
        await page.locator(target).first.fill("")
        await page.locator(target).first.type(str(args["text"]), delay=12)
        if args.get("submit"):
            await page.locator(target).first.press("Enter")
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:  # pragma: no cover - SPA navigations
                pass
        await page.wait_for_timeout(120)
        shot = await _safe_screenshot(ctx, page, f"type into {target}")
        summary = await _page_summary(page)
        return ToolResult(
            ok=True,
            summary=f"Typed into {target}: {str(args['text'])[:60]!r}",
            data={**summary, "screenshots": [shot] if shot else []},
            extracted={"selector": target, "value": str(args["text"]), "url": summary["url"]},
            screenshots=[shot] if shot else [],
            url=summary["url"],
        )


class BrowserPressTool(_BrowserToolBase):
    name = "browser_press"
    description = "Press a keyboard key on an element (or the page), e.g. Enter or Escape."

    @property
    def args_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Key name, e.g. Enter, Tab, Escape"},
                "selector": {"type": "string", "description": "Optional target element"},
            },
            "required": ["key"],
            "additionalProperties": False,
        }

    @property
    def examples(self) -> list[dict[str, Any]]:
        return [{"key": "Enter", "selector": "#search"}]

    async def _run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = await self.session(ctx)
        page = await session.ensure_started()
        key = str(args["key"])
        if args.get("selector"):
            target = await _resolve_selector(page, str(args["selector"]))
            await page.locator(target).first.press(key)
        else:
            await page.keyboard.press(key)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=4000)
        except Exception:  # pragma: no cover
            pass
        await page.wait_for_timeout(120)
        shot = await _safe_screenshot(ctx, page, f"press {key}")
        summary = await _page_summary(page)
        return ToolResult(
            ok=True,
            summary=f"Pressed {key} — now on {summary['url']}",
            data={**summary, "screenshots": [shot] if shot else []},
            extracted={"key": key, "url": summary["url"]},
            screenshots=[shot] if shot else [],
            url=summary["url"],
        )


class BrowserFillFormTool(_BrowserToolBase):
    name = "browser_fill_form"
    description = (
        "Fill several form fields at once. Pass `fields` as a list of "
        "{selector, value, type?}. This does NOT submit. Set submit=true (which the "
        "policy engine treats as high risk) to also click the submit button."
    )
    requires_approval_hint = True

    def keyword_risk_ceiling(self, args: dict[str, Any]) -> RiskLevel:
        # Filling fields is reversible; submitting is not. That distinction is
        # exactly why the plan models them as two separate steps.
        return RiskLevel.HIGH if args.get("submit") else RiskLevel.MEDIUM

    @property
    def args_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "fields": {
                    "type": "array",
                    "description": "List of {selector, value, type?}; "
                    "type is text|select|checkbox",
                    "items": {
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string"},
                            "value": {},
                            "type": {"type": "string", "default": "text"},
                        },
                        "required": ["selector", "value"],
                    },
                },
                "submit": {"type": "boolean", "default": False},
                "submit_selector": {
                    "type": "string",
                    "description": "Button to click when submit=true",
                },
            },
            "required": ["fields"],
            "additionalProperties": False,
        }

    @property
    def examples(self) -> list[dict[str, Any]]:
        return [
            {
                "fields": [
                    {"selector": "#patient_name", "value": "Anita Rao"},
                    {"selector": "#test_type", "value": "Lipid Panel", "type": "select"},
                ],
                "submit": False,
            }
        ]

    async def _run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = await self.session(ctx)
        page = await session.ensure_started()
        filled: list[str] = []
        for field in args.get("fields") or []:
            if not isinstance(field, dict):
                continue
            target = await _resolve_selector(page, str(field.get("selector") or ""))
            value = field.get("value", "")
            kind = str(field.get("type") or "text").lower()
            locator = page.locator(target).first
            if kind == "select":
                await locator.select_option(str(value))
            elif kind == "checkbox":
                truthy = (
                    value
                    if isinstance(value, bool)
                    else str(value).lower() in {"1", "true", "yes", "on"}
                )
                await locator.set_checked(truthy)
            else:
                await locator.fill(str(value))
            filled.append(target)
            await ctx.notify("tool_call", f"filled {target}", selector=target)
        shot = await _safe_screenshot(ctx, page, "form filled")
        summary_text = f"Filled {len(filled)} field(s): {', '.join(filled[:8])}"
        if args.get("submit"):
            selector = args.get("submit_selector") or "button[type=submit]"
            target = await _resolve_selector(page, str(selector))
            await page.locator(target).first.click()
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:  # pragma: no cover
                pass
            await page.wait_for_timeout(200)
            shot = await _safe_screenshot(ctx, page, "form submitted")
            summary_text += f" and submitted via {target}"
        page_summary = await _page_summary(page)
        return ToolResult(
            ok=True,
            summary=summary_text,
            data={**page_summary, "screenshots": [shot] if shot else [], "filled": filled},
            extracted={
                "filled": filled,
                "submitted": bool(args.get("submit")),
                "url": page_summary["url"],
            },
            screenshots=[shot] if shot else [],
            url=page_summary["url"],
        )


class BrowserExtractTool(_BrowserToolBase):
    name = "browser_extract"
    read_only = True
    description = (
        "Read structured data out of the current page. Use it to scrape lists, "
        "tables and prices. Returns `items` (up to 60) plus the page text."
    )

    @property
    def args_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "item_selector": {
                    "type": "string",
                    "description": "CSS selector matching each item, e.g. '.product-card'",
                },
                "fields": {
                    "type": "object",
                    "description": "Map of output field -> CSS selector relative to each item",
                    "additionalProperties": {"type": "string"},
                },
                "text_selector": {
                    "type": "string",
                    "description": "Optional selector whose innerText to return",
                },
                "attribute": {
                    "type": "string",
                    "description": "Return this attribute instead of inner text",
                },
                "limit": {"type": "integer", "default": 20, "description": "Max items"},
            },
            "additionalProperties": False,
        }

    @property
    def examples(self) -> list[dict[str, Any]]:
        return [
            {
                "item_selector": ".product-card",
                "fields": {"name": ".product-name", "price": ".price"},
                "limit": 5,
            }
        ]

    async def _run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = await self.session(ctx)
        page = await session.ensure_started()
        limit = min(int(args.get("limit") or 20), MAX_ITEMS)
        item_selector = args.get("item_selector")
        field_map = args.get("fields") or {}
        attribute = args.get("attribute")

        items: list[dict[str, Any]] = []
        if item_selector:
            nodes = page.locator(str(item_selector))
            count = min(await nodes.count(), limit)
            for i in range(count):
                node = nodes.nth(i)
                record: dict[str, Any] = {}
                if field_map:
                    for out_key, sel in field_map.items():
                        try:
                            loc = node.locator(str(sel)).first
                            if await loc.count() == 0:
                                record[out_key] = None
                                continue
                            raw = (
                                await loc.get_attribute(str(attribute))
                                if attribute
                                else await loc.inner_text()
                            )
                            record[out_key] = (raw or "").strip()[:400]
                        except Exception:  # pragma: no cover - defensive
                            record[out_key] = None
                else:
                    raw = await node.inner_text()
                    record = {"text": (raw or "").strip()[:400]}
                items.append(record)

        page_summary = await _page_summary(page)
        if args.get("text_selector"):
            try:
                text = await page.locator(str(args["text_selector"])).first.inner_text()
                page_summary["text"] = text[:MAX_TEXT_CHARS]
            except Exception:  # pragma: no cover
                pass

        # Anything scraped from a live page is untrusted by definition.
        sanitized = sanitize_untrusted(str(items), max_chars=MAX_TEXT_CHARS)
        shot = await _safe_screenshot(ctx, page, "extract")
        return ToolResult(
            ok=True,
            summary=f"Extracted {len(items)} item(s) from {page.url}",
            data={
                **page_summary,
                "items": items,
                "screenshots": [shot] if shot else [],
                "security_flags": sanitized.flags,
            },
            extracted={"items": items, "count": len(items), "url": page_summary["url"]},
            screenshots=[shot] if shot else [],
            url=page_summary["url"],
            needs_attention=sanitized.suspicious,
            reasons=sanitized.flags,
        )


class BrowserScreenshotTool(_BrowserToolBase):
    name = "browser_screenshot"
    read_only = True
    description = "Capture the current page as a PNG (for the report and audit trail)."

    @property
    def args_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "What this shot shows"},
                "full_page": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        }

    @property
    def examples(self) -> list[dict[str, Any]]:
        return [{"label": "cart with cable added", "full_page": True}]

    async def _run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = await self.session(ctx)
        page = await session.ensure_started()
        label = str(args.get("label") or "manual")
        shot = await _safe_screenshot(ctx, page, label, full_page=bool(args.get("full_page", True)))
        return ToolResult(
            ok=bool(shot),
            summary=f"Captured screenshot {label!r}" if shot else "Screenshot failed",
            data={"src": shot, "url": page.url},
            screenshots=[shot] if shot else [],
            url=page.url,
        )


class BrowserCloseTool(_BrowserToolBase):
    name = "browser_close"
    read_only = True
    description = "Close this run's browser context and release resources."

    @property
    def args_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    async def _run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        await get_browser_manager().release(ctx.run_id)
        return ToolResult(ok=True, summary="Browser closed", data={})


__all__ = [
    "BrowserClickTool",
    "BrowserCloseTool",
    "BrowserExtractTool",
    "BrowserFillFormTool",
    "BrowserManager",
    "BrowserOpenTool",
    "BrowserPressTool",
    "BrowserScreenshotTool",
    "BrowserSession",
    "BrowserTypeTool",
    "get_browser_manager",
    "reset_browser_manager",
]
