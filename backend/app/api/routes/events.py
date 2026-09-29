"""Server-Sent Events: the live feed behind the terminal panel and the timeline.

Two guarantees the frontend depends on:

* **Resumability.** ``Last-Event-ID`` (header *or* ``?last_event_id=``) replays
  exactly the events the client missed, from the monotonic ``events.id`` column.
* **Ordering + no duplicates.** The backlog and the live queue are joined at a
  single cursor, so a client that reconnects mid-run never sees an event twice
  and never sees a gap.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from app.core.db import get_sessionmaker
from app.core.events import fetch_events, stream_run
from app.core.logging import get_logger

router = APIRouter(prefix="/api/runs", tags=["events"])
logger = get_logger(__name__)

# Proxies (nginx, Render, Cloudflare) buffer by default; these headers stop that.
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.get("/{run_id}/events", summary="Live run events (SSE)")
async def run_events(
    run_id: str,
    request: Request,
    last_event_id: int | None = Query(default=None, alias="last_event_id"),
    last_event_id_header: str | None = Header(default=None, alias="Last-Event-ID"),
    types: str | None = Query(default=None, description="Comma-separated event types"),
) -> StreamingResponse:
    """Stream a run's events. Safe to reconnect at any time."""
    cursor = _parse_cursor(last_event_id, last_event_id_header)
    wanted = [t.strip() for t in types.split(",") if t.strip()] if types else None
    session_factory = get_sessionmaker()

    async def generator() -> AsyncIterator[str]:
        # Tell the client where to resume from immediately.
        yield f"event: open\ndata: {run_id}\n\n"
        try:
            async for frame in stream_run(
                session_factory, run_id, last_event_id=cursor, types=wanted
            ):
                if await request.is_disconnected():
                    break
                if frame is None:
                    yield ": keep-alive\n\n"  # heartbeat comment
                    continue
                yield "".join(f"{k}: {v}\n" for k, v in frame.items()) + "\n"
        except asyncio.CancelledError:  # pragma: no cover - client disconnect
            raise
        except Exception as exc:  # noqa: BLE001 - never kill the stream silently
            logger.warning("SSE stream error for run %s: %s", run_id, exc)
            yield f"event: error\ndata: {exc!s}\n\n"
        yield "event: done\ndata: stream closed\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


def _parse_cursor(query_value: int | None, header_value: str | None) -> int:
    """Prefer the query param, fall back to the header, ignore nonsense."""
    for candidate in (query_value, header_value):
        if candidate is None:
            continue
        try:
            return int(str(candidate).strip())
        except (TypeError, ValueError):
            continue
    return 0


@router.get("/{run_id}/events/history", summary="Paged event history")
async def event_history(
    run_id: str,
    after_id: int | None = Query(default=None, alias="after_id"),
    limit: int = Query(default=200, ge=1, le=2000),
    types: str | None = Query(default=None),
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
) -> dict[str, Any]:
    """Non-streaming access to the same audit log (used by the Audit page)."""
    wanted = [t.strip() for t in types.split(",") if t.strip()] if types else None
    async with get_sessionmaker()() as session:
        records = await fetch_events(
            session,
            run_id,
            after_id=after_id,
            limit=limit,
            types=wanted,
            ascending=(order == "asc"),
        )
    return {
        "run_id": run_id,
        "count": len(records),
        "after_id": after_id or 0,
        "items": [
            {
                "id": r.id,
                "run_id": r.run_id,
                "step_id": r.step_id,
                "type": r.type,
                "message": r.message,
                "payload": r.payload,
                "created_at": r.created_at.isoformat(),
            }
            for r in records
        ],
    }


__all__ = ["router"]
