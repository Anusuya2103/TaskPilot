"""Tool package: built-in capabilities + MCP bridge.

:func:`register_builtin_tools` is the single entry point that populates the
registry. It is idempotent, so both app startup and tests can call it.
"""

from __future__ import annotations

from app.tools.base import EventSink, Tool, ToolContext
from app.tools.registry import ToolRegistry, get_registry, reset_registry

_BUILTINS_REGISTERED = False


def register_builtin_tools(*, force: bool = False) -> ToolRegistry:
    """Register every built-in tool. Safe to call repeatedly."""
    global _BUILTINS_REGISTERED
    registry = get_registry()
    if _BUILTINS_REGISTERED and not force:
        return registry

    from app.tools.browser import (
        BrowserClickTool,
        BrowserCloseTool,
        BrowserExtractTool,
        BrowserFillFormTool,
        BrowserOpenTool,
        BrowserPressTool,
        BrowserScreenshotTool,
        BrowserTypeTool,
    )
    from app.tools.files import FilesListTool, FilesReadTool, FilesWriteTool
    from app.tools.http_api import HttpApiTool, HttpFetchTool
    from app.tools.report import ReportWriteTool
    from app.tools.state import NoteTool, TaskCompleteTool

    registry.register_many(
        [
            # --- browser ------------------------------------------------------
            BrowserOpenTool(),
            BrowserClickTool(),
            BrowserTypeTool(),
            BrowserPressTool(),
            BrowserFillFormTool(),
            BrowserExtractTool(),
            BrowserScreenshotTool(),
            BrowserCloseTool(),
            # --- http ---------------------------------------------------------
            HttpApiTool(),
            HttpFetchTool(),
            # --- files --------------------------------------------------------
            FilesReadTool(),
            FilesWriteTool(),
            FilesListTool(),
            # --- reporting / bookkeeping --------------------------------------
            ReportWriteTool(),
            NoteTool(),
            TaskCompleteTool(),
        ]
    )
    _BUILTINS_REGISTERED = True
    return registry


def reset_builtins() -> None:
    """Test helper: forget that built-ins were registered."""
    global _BUILTINS_REGISTERED
    _BUILTINS_REGISTERED = False


__all__ = [
    "EventSink",
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "get_registry",
    "register_builtin_tools",
    "reset_builtins",
    "reset_registry",
]
