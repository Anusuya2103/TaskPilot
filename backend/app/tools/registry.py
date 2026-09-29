"""Central tool registry.

One place answers "what can the agent do?". Built-in tools register at import
time; MCP tools are injected later by :class:`app.tools.mcp_bridge.MCPManager`.
Because both satisfy :class:`~app.tools.base.Tool`, the planner and executor are
oblivious to where a capability came from.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.core.errors import ToolNotFoundError
from app.core.logging import get_logger
from app.core.models import RiskLevel
from app.tools.base import Tool

logger = get_logger(__name__)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._mcp_servers: list[dict[str, Any]] = []

    # --- registration ---------------------------------------------------------
    def register(self, tool: Tool, *, replace: bool = True) -> None:
        if tool.name in self._tools and not replace:
            raise ValueError(f"tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool
        logger.debug("registered tool %s", tool.name)

    def register_many(self, tools: Sequence[Tool]) -> None:
        for tool in tools:
            self.register(tool)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def replace_source(self, source: str, tools: Sequence[Tool]) -> None:
        """Swap out every tool from a given source (used when MCP reconnects)."""
        for name in [n for n, t in self._tools.items() if t.source == source]:
            self._tools.pop(name, None)
        self.register_many(tools)

    def clear(self) -> None:
        self._tools.clear()
        self._mcp_servers.clear()

    # --- lookup ---------------------------------------------------------------
    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            close = self.suggest(name)
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            raise ToolNotFoundError(
                f"Unknown tool {name!r}.{hint}",
                details={"available": sorted(self._tools), "suggestions": close},
            )
        return tool

    def has(self, name: str) -> bool:
        return name in self._tools

    def suggest(self, name: str, limit: int = 3) -> list[str]:
        """Cheap fuzzy suggestion so a bad plan can self-correct in one retry."""
        from difflib import get_close_matches

        return get_close_matches(name, list(self._tools), n=limit, cutoff=0.5)

    def all(self, *, available_only: bool = False) -> list[Tool]:
        tools = sorted(self._tools.values(), key=lambda t: (t.source != "builtin", t.name))
        if available_only:
            tools = [t for t in tools if t.available]
        return tools

    def names(self, *, available_only: bool = True) -> list[str]:
        return [t.name for t in self.all(available_only=available_only)]

    def names_by_risk(self, level: RiskLevel) -> list[str]:
        return [t.name for t in self.all() if t.risk_level == level]

    # --- MCP bookkeeping ------------------------------------------------------
    def set_mcp_servers(self, servers: list[dict[str, Any]]) -> None:
        self._mcp_servers = servers

    @property
    def mcp_servers(self) -> list[dict[str, Any]]:
        return list(self._mcp_servers)

    # --- prompt helpers -------------------------------------------------------
    def prompt_catalog(self, *, only: list[str] | None = None) -> str:
        """Tool catalogue block injected into the planner prompt."""
        tools = self.all(available_only=True)
        if only:
            tools = [t for t in tools if t.name in set(only)]
        return "\n".join(t.prompt_description() for t in tools)

    def describe_all(self) -> list[dict[str, Any]]:
        return [t.describe() for t in self.all()]

    @property
    def high_risk_tools(self) -> list[str]:
        return self.names_by_risk(RiskLevel.HIGH)


_registry: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry


def reset_registry() -> None:
    """Test helper: wipe and re-register the built-ins."""
    global _registry
    _registry = None


__all__ = ["ToolRegistry", "get_registry", "reset_registry"]
