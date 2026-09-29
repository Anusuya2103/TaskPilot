"""Shared FastAPI dependencies: auth, rate limiting and the orchestrator."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Header, HTTPException, Request, status

from app.agent.orchestrator import get_orchestrator
from app.core.config import Settings, get_settings
from app.core.security import get_limiter


def settings_dep() -> Settings:
    return get_settings()


def orchestrator_dep() -> Any:
    return get_orchestrator()


async def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    settings: Settings = Depends(settings_dep),
) -> None:
    """Optional shared-secret auth.

    Disabled by default so the public demo works out of the box; enabling it in
    production is a one-line env change and is documented in the README.
    """
    if not settings.api_key:
        return
    if x_api_key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "Invalid or missing X-API-Key"},
        )


async def rate_limit(
    request: Request,
    settings: Settings = Depends(settings_dep),
) -> None:
    """Per-client token bucket. Returns 429 with a Retry-After hint."""
    if settings.rate_limit_per_minute <= 0:
        return
    client = request.client.host if request.client else "unknown"
    allowed, remaining = get_limiter().check(f"{client}:{request.url.path}")
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "rate_limited",
                "message": "Too many requests — slow down a little.",
                "retryable": True,
            },
            headers={"Retry-After": "60"},
        )
    request.state.rate_remaining = remaining


__all__ = [
    "orchestrator_dep",
    "rate_limit",
    "require_api_key",
    "settings_dep",
]
