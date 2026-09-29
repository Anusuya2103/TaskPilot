"""Health and readiness probes.

``/health`` is liveness (fast, never touches the DB). ``/ready`` is readiness:
it checks the database, the tool registry and the MCP bridge, and reports
degraded rather than failing so a demo survives one broken MCP server.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Response

from app import __version__
from app.api.deps import settings_dep
from app.core.config import Settings, get_settings
from app.core.db import ping
from app.core.models import EventType
from app.core.schemas import HealthOut
from app.tools import register_builtin_tools
from app.tools.mcp_bridge import get_mcp_manager

router = APIRouter(tags=["health"])

STARTED_AT = time.time()


@router.get("/health", response_model=HealthOut, summary="Liveness probe")
async def health() -> HealthOut:
    """Cheap liveness check — deliberately does not touch the database."""
    settings = get_settings()
    return HealthOut(
        status="ok",
        version=__version__,
        env=settings.env,
        uptime_seconds=round(time.time() - STARTED_AT, 1),
        checks={"api": "ok"},
    )


@router.get("/ready", summary="Readiness probe")
async def ready(response: Response, settings: Settings = Depends(settings_dep)) -> dict[str, Any]:
    """Readiness: database reachable, tools registered, MCP reported."""
    register_builtin_tools()
    from app.tools.registry import get_registry

    registry = get_registry()
    db_ok = await ping()
    mcp = get_mcp_manager()
    mcp_status = mcp.status()
    mcp_ok = all(s["status"] == "connected" for s in mcp_status) if mcp_status else True

    checks: dict[str, Any] = {
        "database": {"ok": db_ok},
        "tools": {"ok": len(registry.all()) > 0, "count": len(registry.all())},
        "mcp": {
            "ok": mcp_ok,
            "servers": mcp_status,
        },
        "llm": {
            "ok": True,
            "provider": settings.llm_provider,
            # Surfaced so a demo running on the fallback brain is never mistaken
            # for a real model.
            "effective_provider": (
                "scripted"
                if settings.llm_provider != "scripted"
                and not settings.provider_key(settings.llm_provider)
                and settings.allow_scripted_fallback
                else settings.llm_provider
            ),
        },
    }
    healthy = db_ok and checks["tools"]["ok"]
    response.status_code = 200 if healthy else 503
    return {
        "status": "ok" if healthy else "degraded",
        "version": __version__,
        "uptime_seconds": round(time.time() - STARTED_AT, 1),
        "checks": checks,
    }


@router.get("/meta/event-types", summary="Event type vocabulary")
async def event_types() -> dict[str, list[str]]:
    return {"event_types": [e.value for e in EventType]}
