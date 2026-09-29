"""MCP bridge — expose Model Context Protocol servers as first-class tools.

Uses the official ``mcp`` Python SDK. Each configured server is launched (stdio)
or dialled (SSE), its ``tools/list`` is introspected, and every remote tool is
wrapped in an :class:`MCPToolProxy` that satisfies the same
:class:`~app.tools.base.Tool` contract as the built-ins.

Failure policy: an MCP server that fails to start is *reported*, not fatal.
The demo must keep working when ``npx``/``uvx`` are unavailable or a sandbox has
no network — that is the difference between a demo that survives the room's
wifi and one that does not.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings, get_settings
from app.core.errors import ToolError
from app.core.logging import get_logger, log_event
from app.core.models import RiskLevel
from app.core.schemas import ToolResult
from app.core.security import sanitize_untrusted
from app.tools.base import Tool, ToolContext

logger = get_logger(__name__)


@dataclass
class MCPServerState:
    """Runtime status for one configured server (surfaced on /api/tools)."""

    name: str
    transport: str = "stdio"
    command: str | None = None
    args: list[str] = field(default_factory=list)
    url: str | None = None
    status: str = "disconnected"   # disconnected|connecting|connected|error
    error: str | None = None
    tools: list[str] = field(default_factory=list)
    latency_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "transport": self.transport,
            "command": self.command,
            "args": self.args,
            "url": self.url,
            "status": self.status,
            "error": self.error,
            "tools": self.tools,
            "latency_ms": self.latency_ms,
            "tool_count": len(self.tools),
        }


class MCPToolProxy(Tool):
    """Adapts one remote MCP tool to the local Tool interface."""

    source = "mcp"

    def __init__(
        self,
        *,
        server: str,
        remote_name: str,
        description: str,
        schema: dict[str, Any],
        invoke: Any,
        destructive: bool = False,
    ) -> None:
        self.server = server
        self.remote_name = remote_name
        self.name = self._local_name(server, remote_name)
        self.description = description or f"MCP tool {remote_name} on {server}"
        self._schema = schema or {"type": "object", "properties": {}}
        self._invoke = invoke
        self.category = "mcp"
        # Remote tools are conservatively medium risk: we did not write them, so
        # we do not get to assume they are read-only.
        self.risk_level = RiskLevel.HIGH if destructive else RiskLevel.MEDIUM

    @staticmethod
    def _local_name(server: str, remote_name: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in server)
        return f"{safe}__{remote_name}"

    @property
    def args_schema(self) -> dict[str, Any]:
        return self._schema

    async def _run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        result = await self._invoke(args)
        if result is None:
            return ToolResult(ok=False, summary=f"{self.name} returned nothing", error="empty")

        text_parts: list[str] = []
        structured: Any = None
        images: list[str] = []
        for content in getattr(result, "content", []) or []:
            ctype = getattr(content, "type", None)
            if ctype == "text":
                text_parts.append(getattr(content, "text", ""))
            elif ctype == "image":
                images.append(getattr(content, "data", "")[:64] + "…")
            elif ctype == "resource":
                text_parts.append(str(getattr(content, "resource", "")))
            else:
                text_parts.append(str(content))

        structured = getattr(result, "structuredContent", None)
        raw_text = "\n".join(text_parts)
        sanitized = sanitize_untrusted(raw_text, max_chars=20_000)
        if structured is None and sanitized.text.lstrip()[:1] in {"{", "["}:
            try:
                structured = json.loads(sanitized.text)
            except json.JSONDecodeError:
                structured = None

        is_error = bool(getattr(result, "isError", False))
        preview = sanitized.text[:4000]
        return ToolResult(
            ok=not is_error,
            summary=preview or f"{self.name} completed",
            data={
                "server": self.server,
                "tool": self.remote_name,
                "text": preview,
                "structured": structured,
                "images": images,
                "security_flags": sanitized.flags,
            },
            extracted={"text": preview, "structured": structured},
            needs_attention=sanitized.suspicious or is_error,
            reasons=sanitized.flags,
            untrusted_content=True,
            error=preview if is_error else None,
        )


class MCPManager:
    """Owns the long-lived MCP client sessions for the whole process."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.servers: dict[str, MCPServerState] = {}
        self._sessions: dict[str, Any] = {}          # name -> ClientSession
        self._stacks: dict[str, AsyncExitStack] = {}  # name -> exit stack
        self._tools: list[MCPToolProxy] = []
        self._lock = asyncio.Lock()
        self.started = False

    # --- lifecycle ------------------------------------------------------------
    async def start(self, *, force: bool = False) -> list[MCPToolProxy]:
        """Connect every configured server. Never raises."""
        async with self._lock:
            if self.started and not force:
                return self._tools
            if force:
                await self._shutdown_locked()
            self.started = True
            self._tools = []

            if not self.settings.mcp_enabled:
                logger.info("mcp disabled by configuration")
                return []

            try:
                configs = self.settings.mcp_server_configs()
            except ValueError as exc:
                logger.warning("invalid TASKPILOT_MCP_SERVERS: %s", exc)
                return []

            if not configs:
                logger.info("no MCP servers configured")
                return []

            results = await asyncio.gather(
                *(self._connect_one(cfg) for cfg in configs), return_exceptions=True
            )
            for cfg, result in zip(configs, results, strict=False):
                if isinstance(result, BaseException):
                    logger.warning("MCP server %s failed: %s", cfg.get("name"), result)
                elif result:
                    self._tools.extend(result)
            return self._tools

    async def _connect_one(self, cfg: dict[str, Any]) -> list[MCPToolProxy]:
        name = str(cfg["name"])
        transport = str(cfg.get("transport") or "stdio").lower()
        state = MCPServerState(
            name=name,
            transport=transport,
            command=cfg.get("command"),
            args=list(cfg.get("args") or []),
            url=cfg.get("url"),
        )
        self.servers[name] = state

        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.sse import sse_client
            from mcp.client.stdio import stdio_client
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            state.status = "error"
            state.error = f"mcp SDK unavailable: {exc}"
            return []

        stack = AsyncExitStack()
        loop = asyncio.get_event_loop()
        started = loop.time()
        try:
            state.status = "connecting"
            timeout = self.settings.mcp_startup_timeout_seconds

            if transport in {"sse", "http"}:
                if not state.url:
                    raise ToolError(f"MCP server {name!r} needs a `url` for SSE transport")
                streams = await stack.enter_async_context(sse_client(state.url))
            else:
                if not state.command:
                    raise ToolError(f"MCP server {name!r} needs a `command` for stdio transport")
                params = StdioServerParameters(
                    command=str(state.command),
                    args=[str(a) for a in state.args],
                    env={str(k): str(v) for k, v in (cfg.get("env") or {}).items()} or None,
                )
                streams = await stack.enter_async_context(stdio_client(params))
            read_stream, write_stream = streams

            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await asyncio.wait_for(session.initialize(), timeout=timeout)

            listing = await asyncio.wait_for(session.list_tools(), timeout=timeout)
            proxies: list[MCPToolProxy] = []
            for tool in getattr(listing, "tools", []) or []:
                proxy = self._wrap(name, session, tool)
                proxies.append(proxy)
                state.tools.append(proxy.name)

            self._sessions[name] = session
            self._stacks[name] = stack
            state.status = "connected"
            state.latency_ms = round((loop.time() - started) * 1000, 1)
            log_event(
                logger,
                logging.INFO,
                f"mcp connected: {name}",
                tool_count=len(proxies),
                latency_ms=state.latency_ms,
            )
            return proxies
        except Exception as exc:  # noqa: BLE001 - degrade, never crash startup
            state.status = "error"
            state.error = f"{type(exc).__name__}: {exc}"[:400]
            try:
                await asyncio.shield(stack.aclose())
            except Exception:  # pragma: no cover - best effort
                pass
            logger.warning("MCP server %s unavailable: %s", name, state.error)
            return []

    def _wrap(self, server: str, session: Any, tool: Any) -> MCPToolProxy:
        remote_name = getattr(tool, "name", "unknown")
        schema = getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}}
        description = getattr(tool, "description", "") or ""
        annotations = getattr(tool, "annotations", None)
        destructive = bool(
            getattr(annotations, "destructiveHint", False)
            or getattr(annotations, "destructive_hint", False)
        )

        async def invoke(args: dict[str, Any]) -> Any:
            return await asyncio.wait_for(
                session.call_tool(remote_name, arguments=args or {}),
                timeout=self.settings.mcp_call_timeout_seconds,
            )

        return MCPToolProxy(
            server=server,
            remote_name=remote_name,
            description=description,
            schema=dict(schema),
            invoke=invoke,
            destructive=destructive,
        )

    async def _shutdown_locked(self) -> None:
        for name, stack in list(self._stacks.items()):
            try:
                await stack.aclose()
            except Exception as exc:  # pragma: no cover - best effort
                logger.debug("mcp shutdown %s: %s", name, exc)
            state = self.servers.get(name)
            if state:
                state.status = "disconnected"
        self._stacks.clear()
        self._sessions.clear()
        self._tools = []

    async def stop(self) -> None:
        async with self._lock:
            await self._shutdown_locked()
            self.started = False

    # --- introspection --------------------------------------------------------
    @property
    def tools(self) -> list[MCPToolProxy]:
        return list(self._tools)

    def status(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self.servers.values()]


_manager: MCPManager | None = None


def get_mcp_manager() -> MCPManager:
    global _manager
    if _manager is None:
        _manager = MCPManager()
    return _manager


async def reset_mcp_manager() -> None:
    """Test helper: stop and forget the manager."""
    global _manager
    if _manager is not None:
        await _manager.stop()
    _manager = None


__all__ = [
    "MCPManager",
    "MCPServerState",
    "MCPToolProxy",
    "get_mcp_manager",
    "reset_mcp_manager",
]
