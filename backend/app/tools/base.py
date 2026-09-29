"""The Tool abstraction every capability implements.

Why an ABC rather than a protocol-with-loose-functions: the planner, policy
engine and UI all need *declared* metadata (risk level, JSON schema, examples)
before anything runs. That metadata is the contract the LLM plans against, and
it is also what makes MCP tools indistinguishable from built-ins at runtime.
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.core.errors import RunCancelled, ToolError, ToolTimeoutError
from app.core.logging import get_logger, log_event
from app.core.models import EventType, RiskLevel
from app.core.schemas import ToolResult
from app.core.security import sanitize_untrusted

logger = get_logger(__name__)

EventSink = Callable[[EventType | str, str, dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class ToolContext:
    """Everything a tool is allowed to know about the run it serves.

    Tools never import the orchestrator, the DB or the LLM: they receive this
    context. That keeps them independently testable and keeps the dependency
    arrow pointing one way.
    """

    run_id: str
    step_id: str | None = None
    settings: Settings = field(default_factory=get_settings)
    emit: EventSink | None = None
    artifact_dir: Path = field(default_factory=lambda: get_settings().artifact_path)
    screenshots_dir: Path = field(default_factory=lambda: get_settings().screenshots_path)
    state: dict[str, Any] = field(default_factory=dict)
    cancel_check: Callable[[], bool] | None = None
    idempotency_key: str | None = None

    async def notify(self, event: EventType | str, message: str, **payload: Any) -> None:
        if self.emit is not None:
            await self.emit(event, message, payload)

    def cancelled(self) -> bool:
        return bool(self.cancel_check and self.cancel_check())

    def raise_if_cancelled(self) -> None:
        if self.cancelled():
            raise RunCancelled("Run was cancelled by the user")


class Tool(abc.ABC):
    """Base class for every capability the agent can invoke."""

    name: str = "tool"
    description: str = ""
    risk_level: RiskLevel = RiskLevel.LOW
    category: str = "general"
    requires_approval_hint: bool = False
    source: str = "builtin"          # builtin | mcp
    server: str | None = None
    available: bool = True
    timeout_seconds: float | None = None
    #: True when the tool *cannot* change remote or local state. Read-only tools
    #: are exempt from keyword-driven risk, because "Open the checkout page" and
    #: "Place the order" differ entirely in effect, not in vocabulary.
    read_only: bool = False

    def keyword_risk_ceiling(self, args: dict[str, Any]) -> RiskLevel:
        """Highest risk that *keyword analysis alone* may assign to this step.

        The policy engine never lets language escalate a read-only action past
        this ceiling, which is what keeps the approval gate from firing on
        "open the checkout page" while still firing on "click Place order".
        Structural rules (submit flags, HTTP verbs, file overwrites) are *not*
        capped — they describe the effect directly.
        """
        return RiskLevel.LOW if self.read_only else RiskLevel.HIGH

    # --- declarative metadata -------------------------------------------------
    @property
    def args_schema(self) -> dict[str, Any]:
        """JSON schema used for (a) the planner prompt and (b) arg validation."""
        return {"type": "object", "properties": {}, "additionalProperties": True}

    @property
    def examples(self) -> list[dict[str, Any]]:
        return []

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "risk_level": self.risk_level.value,
            "category": self.category,
            "source": self.source,
            "server": self.server,
            "available": self.available,
            "args_schema": self.args_schema,
            "examples": self.examples,
        }

    def prompt_description(self) -> str:
        """Compact, token-efficient description for the planner prompt."""
        lines = [f"- {self.name} (risk={self.risk_level.value}): {self.description}"]
        props = self.args_schema.get("properties") or {}
        if props:
            required = set(self.args_schema.get("required") or [])
            parts = []
            for key, spec in list(props.items())[:14]:
                marker = "*" if key in required else ""
                kind = spec.get("type", "any") if isinstance(spec, dict) else "any"
                desc = (spec.get("description", "") if isinstance(spec, dict) else "") or ""
                desc = f" — {desc}" if desc else ""
                parts.append(f"{key}{marker}:{kind}{desc}")
            lines.append("  args: " + "; ".join(parts))
        if self.examples:
            try:
                sample = json.dumps(self.examples[:1], default=str)[:280]
                lines.append(f"  example args: {sample}")
            except (TypeError, ValueError):  # pragma: no cover
                pass
        return "\n".join(lines)

    # --- validation -----------------------------------------------------------
    def validate_args(self, args: dict[str, Any]) -> dict[str, Any]:
        """Light-weight schema check. Missing required args fail fast and loudly.

        We deliberately do not pull in ``jsonschema``: the planner prompt is
        schema-driven, and the executor needs a *clear error message* more than a
        standards-complete validator.
        """
        if not isinstance(args, dict):
            raise ToolError(f"{self.name}: args must be an object, got {type(args).__name__}")
        schema = self.args_schema or {}
        required = schema.get("required") or []
        missing = [k for k in required if k not in args or args.get(k) in (None, "")]
        if missing:
            raise ToolError(
                f"{self.name}: missing required argument(s): {', '.join(missing)}",
                details={"missing": missing, "schema": schema},
            )
        props = schema.get("properties") or {}
        allow_extra = schema.get("additionalProperties", True)
        if props and not allow_extra:
            unknown = [k for k in args if k not in props]
            if unknown:
                raise ToolError(
                    f"{self.name}: unknown argument(s): {', '.join(unknown)}",
                    details={"unknown": unknown},
                )
        return args

    # --- execution ------------------------------------------------------------
    @abc.abstractmethod
    async def _run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Tool-specific implementation. Override this, not :meth:`run`."""

    async def _invoke(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        timeout = self.timeout_seconds or ctx.settings.step_timeout_seconds
        try:
            return await asyncio.wait_for(self._run(args, ctx), timeout=timeout)
        except TimeoutError as exc:
            raise ToolTimeoutError(
                f"{self.name} exceeded its {timeout:g}s timeout",
                details={"timeout_seconds": timeout},
            ) from exc

    async def run(self, args: dict[str, Any], ctx: ToolContext | None = None) -> ToolResult:
        """Template method: validate → time → execute → sanitize → normalise."""
        ctx = ctx or ToolContext(run_id="unknown")
        started = time.perf_counter()
        try:
            self.validate_args(args)
        except ToolError as exc:
            latency = round((time.perf_counter() - started) * 1000, 1)
            return ToolResult(
                ok=False,
                summary=exc.message,
                error=exc.message,
                reasons=["validation"],
                latency_ms=latency,
            )
        try:
            result = await self._invoke(args, ctx)
        except ToolTimeoutError as exc:
            result = ToolResult(
                ok=False, summary=str(exc), error=str(exc), reasons=["timeout"]
            )
        except RunCancelled:
            raise
        except ToolError as exc:
            result = ToolResult(
                ok=False, summary=exc.message, error=exc.message, reasons=["tool_error"]
            )
        except Exception as exc:  # noqa: BLE001 - never let a tool kill the run
            logger.exception("tool %s raised", self.name)
            result = ToolResult(
                ok=False,
                summary=f"{self.name} failed: {type(exc).__name__}: {exc}",
                error=f"{type(exc).__name__}: {exc}",
                reasons=["exception"],
            )
        result.latency_ms = round((time.perf_counter() - started) * 1000, 1)
        if result.summary:
            sanitized = sanitize_untrusted(result.summary, keep_suspicious=True)
            result.summary = sanitized.text
            if sanitized.suspicious:
                result.untrusted_content = True
                result.reasons = list(dict.fromkeys(result.reasons + sanitized.flags))
        log_event(
            logger,
            logging.INFO if result.ok else logging.WARNING,
            f"tool:{self.name}",
            run_id=ctx.run_id,
            step_id=ctx.step_id,
            ok=result.ok,
            duration_ms=result.latency_ms,
            reasons=result.reasons,
        )
        return result

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} name={self.name!r} risk={self.risk_level.value}>"


__all__ = ["EventSink", "Tool", "ToolContext"]
