"""Generic HTTP/JSON tool — the "connect to any API" leg of the product.

Safety posture: non-GET methods are declared **medium** risk here so the policy
engine's "non-GET ⇒ requires approval" rule has something to escalate to. The
domain allowlist is enforced before a byte leaves the process.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.core.errors import ToolError
from app.core.models import RiskLevel
from app.core.schemas import ToolResult
from app.core.security import check_url_allowed, sanitize_untrusted
from app.tools.base import Tool, ToolContext

MAX_BODY_CHARS = 200_000
TRUNCATING = 40_000


class HttpApiTool(Tool):
    name = "http_request"
    description = (
        "Call any JSON/HTTP API (GET, POST, PUT, PATCH, DELETE). Use for REST "
        "endpoints and webhooks. Non-GET methods pause for human approval."
    )
    risk_level = RiskLevel.MEDIUM
    category = "api"

    @property
    def args_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Absolute URL"},
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
                    "default": "GET",
                },
                "params": {"type": "object", "description": "Query string parameters"},
                "json_body": {"type": "object", "description": "JSON request body"},
                "data": {"type": "object", "description": "Form-encoded body"},
                "headers": {"type": "object", "description": "Extra request headers"},
                "timeout_seconds": {"type": "number", "description": "Request timeout"},
            },
            "required": ["url"],
            "additionalProperties": False,
        }

    @property
    def examples(self) -> list[dict[str, Any]]:
        return [
            {"url": "https://httpbin.org/json", "method": "GET"},
            {
                "url": "https://httpbin.org/post",
                "method": "POST",
                "json_body": {"hello": "world"},
            },
        ]

    async def _run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = str(args["url"]).strip()
        host = check_url_allowed(url, ctx.settings)
        method = str(args.get("method") or "GET").upper()

        headers = {
            "User-Agent": "TaskPilot/1.0 (+https://github.com/neuronova/taskpilot)",
            "Accept": "application/json, text/plain;q=0.9, */*;q=0.8",
        }
        for key, value in (args.get("headers") or {}).items():
            headers[str(key)] = str(value)

        timeout = float(args.get("timeout_seconds") or ctx.settings.step_timeout_seconds)
        request_kwargs: dict[str, Any] = {
            "params": args.get("params") or None,
            "headers": headers,
            "timeout": min(timeout, 120.0),
            "follow_redirects": True,
        }
        if method in {"POST", "PUT", "PATCH", "DELETE"}:
            if args.get("json_body") is not None:
                request_kwargs["json"] = args["json_body"]
            elif args.get("data") is not None:
                request_kwargs["data"] = args["data"]

        await ctx.notify("tool_call", f"{method} {url}", host=host, method=method)
        async with httpx.AsyncClient() as client:
            try:
                response = await client.request(method, url, **request_kwargs)
            except httpx.TimeoutException as exc:
                raise ToolError(f"Request to {host} timed out after {timeout:g}s") from exc
            except httpx.HTTPError as exc:
                raise ToolError(f"Request to {host} failed: {exc}") from exc

        body = response.text[:MAX_BODY_CHARS]
        parsed: Any = None
        content_type = response.headers.get("content-type", "")
        if "json" in content_type or body.lstrip()[:1] in {"{", "["}:
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = None
        sanitized = sanitize_untrusted(body, max_chars=TRUNCATING)
        response_summary = {
            "status": response.status_code,
            "url": str(response.url),
            "content_type": content_type,
            "headers": {k: v for k, v in response.headers.items() if k.lower() != "set-cookie"},
            "body_preview": sanitized.text[:8000],
        }
        ok = response.status_code < 400
        return ToolResult(
            ok=ok,
            summary=f"{method} {url} → HTTP {response.status_code} ({len(body)} bytes)",
            data={**response_summary, "json": parsed if isinstance(parsed, (dict, list)) else None},
            extracted={
                "status": response.status_code,
                "json": parsed if isinstance(parsed, (dict, list)) else None,
            },
            url=str(response.url),
            needs_attention=sanitized.suspicious,
            reasons=sanitized.flags,
            error=None if ok else f"HTTP {response.status_code}",
            untrusted_content=True,
        )


class HttpFetchTool(HttpApiTool):
    """Read-only alias that is always low risk (GET only)."""

    name = "http_get"
    description = "Fetch a URL read-only (GET). Never mutates anything — always low risk."
    risk_level = RiskLevel.LOW
    read_only = True

    @property
    def args_schema(self) -> dict[str, Any]:
        # Narrow the parent's schema to the read-only arguments. The intent is to
        # drop the *body* writers (``json_body``/``data``) and force GET.
        #
        # ``timeout_seconds`` must be kept: ``_run`` reads it, so dropping it here
        # made a documented argument fail validation with "unknown argument(s)".
        # ``method`` is intentionally dropped — this alias is GET-only.
        schema = dict(super().args_schema)
        schema["properties"] = {
            k: v
            for k, v in schema["properties"].items()
            if k in {"url", "params", "headers", "timeout_seconds"}
        }
        schema["required"] = ["url"]
        return schema


__all__ = ["HttpApiTool", "HttpFetchTool"]
