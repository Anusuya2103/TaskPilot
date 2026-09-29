"""Tool catalogue and runtime settings.

``GET /api/tools`` is what the planner prompt is built from, and what the UI shows
on the Settings page — one source of truth for "what can the agent do".
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query

from app.agent.llm import provider_status
from app.api.deps import rate_limit, require_api_key, settings_dep
from app.core.config import Settings
from app.core.db import session_scope
from app.core.logging import get_logger
from app.core.models import Setting
from app.core.schemas import DemoTaskOut, RuntimeSettings, SettingsOut, ToolListOut, ToolOut
from app.demo.canned import CANNED_TASKS
from app.tools import register_builtin_tools
from app.tools.mcp_bridge import get_mcp_manager
from app.tools.registry import get_registry

router = APIRouter(prefix="/api", tags=["tools"])
logger = get_logger(__name__)


@router.get("/tools", response_model=ToolListOut, summary="List available tools")
async def list_tools(
    source: str | None = Query(default=None, description="builtin | mcp"),
    available_only: bool = Query(default=True),
) -> ToolListOut:
    register_builtin_tools()
    registry = get_registry()
    tools = registry.all(available_only=available_only)
    if source:
        tools = [t for t in tools if t.source == source]
    return ToolListOut(
        tools=[ToolOut(**t.describe()) for t in tools],
        mcp_servers=get_mcp_manager().status(),
    )


@router.post(
    "/tools/mcp/refresh",
    response_model=ToolListOut,
    summary="Reconnect MCP servers",
    dependencies=[Depends(require_api_key), Depends(rate_limit)],
)
async def refresh_mcp() -> ToolListOut:
    """Re-dial every configured MCP server and refresh the registry."""
    register_builtin_tools()
    manager = get_mcp_manager()
    proxies = await manager.start(force=True)
    get_registry().replace_source("mcp", proxies)
    return ToolListOut(
        tools=[ToolOut(**t.describe()) for t in get_registry().all()],
        mcp_servers=manager.status(),
    )


@router.get("/settings", response_model=SettingsOut, summary="Read settings")
async def get_runtime_settings(settings: Settings = Depends(settings_dep)) -> SettingsOut:
    register_builtin_tools()
    overrides = await _load_overrides()
    merged: dict[str, Any] = {
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
        "llm_temperature": settings.llm_temperature,
        "mcp_enabled": settings.mcp_enabled,
        "mcp_servers": _safe_servers(settings),
        "domain_allowlist": settings.allowlist_patterns,
        "auto_approve_low_risk": settings.auto_approve_low_risk,
        "approval_timeout_seconds": settings.approval_timeout_seconds,
        "max_step_attempts": settings.max_step_attempts,
        "max_replans": settings.max_replans,
        "browser_headless": settings.browser_headless,
        "planner_preview": settings.planner_preview,
    }
    merged.update(overrides)
    return SettingsOut(
        settings=merged,
        providers=provider_status(settings),
        tool_count=len(get_registry().all()),
        # Surfaced in the UI so the toggle cannot be mistaken for optional.
        high_risk_always_requires_approval=True,
    )


@router.put(
    "/settings",
    response_model=SettingsOut,
    summary="Update settings",
    dependencies=[Depends(require_api_key), Depends(rate_limit)],
)
async def update_settings(payload: RuntimeSettings) -> SettingsOut:
    """Persist overrides. High-risk approval is intentionally not configurable."""
    touched = payload.touched()
    if touched:
        async with session_scope() as session:
            row = await session.get(Setting, "runtime")
            merged = dict(row.value) if row is not None else {}
            merged.update(touched)
            if row is None:
                session.add(Setting(key="runtime", value=merged))
            else:
                row.value = merged
    await _apply_overrides()
    return await get_runtime_settings()


@router.get("/demo/tasks", response_model=list[DemoTaskOut], summary="Canned demo tasks")
async def demo_tasks() -> list[DemoTaskOut]:
    return [
        DemoTaskOut(
            id=t.id,
            title=t.title,
            prompt=t.prompt,
            needs_approval=t.needs_approval,
            tools=t.tools,
            estimated_manual_minutes=t.estimated_manual_minutes,
            category=t.category,
            description=t.description,
        )
        for t in CANNED_TASKS
    ]


# ---------------------------------------------------------------------------
# settings persistence
# ---------------------------------------------------------------------------
async def _load_overrides() -> dict[str, Any]:
    async with session_scope() as session:
        row = await session.get(Setting, "runtime")
        return dict(row.value) if row is not None else {}


async def _apply_overrides() -> None:
    """Re-apply DB overrides on top of the environment configuration."""
    from app.core.config import get_settings, reset_settings_cache

    overrides = await _load_overrides()
    if not overrides:
        return
    reset_settings_cache()
    settings = get_settings()
    for key, value in overrides.items():
        if hasattr(settings, key):
            object.__setattr__(settings, key, value)
    if overrides.get("domain_allowlist"):
        settings.domain_allowlist = ",".join(overrides["domain_allowlist"])
    if overrides.get("mcp_servers"):
        settings.mcp_servers = json.dumps(overrides["mcp_servers"])


def _safe_servers(settings: Settings) -> list[dict[str, Any]]:
    try:
        return [
            {k: v for k, v in cfg.items() if k != "env"} for cfg in settings.mcp_server_configs()
        ]
    except ValueError:  # pragma: no cover - misconfigured JSON
        return []


__all__ = ["router"]
