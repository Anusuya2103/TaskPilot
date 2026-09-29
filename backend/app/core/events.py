"""Event persistence + live fan-out.

Every agent transition goes through :func:`emit`, which

1. **persists** a row in ``events`` (the immutable audit trail), and
2. **publishes** it to in-process subscribers (the SSE endpoint).

Because the persisted ``id`` is monotonic per database, ``Last-Event-ID``
resumption is exact: the SSE handler simply replays ``WHERE id > cursor`` and
then switches to the live queue, de-duplicating on ``id``.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger, log_event, scrub
from app.core.models import EventType, RunEvent, utcnow

logger = get_logger(__name__)

# Cap per-subscriber buffers so a stalled browser tab can never OOM the API.
QUEUE_MAXSIZE = 1000
HEARTBEAT_SECONDS = 15.0


@dataclass(slots=True)
class EventRecord:
    """Detached, JSON-ready view of a persisted event."""

    id: int
    run_id: str
    type: str
    message: str
    payload: dict[str, Any]
    step_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)

    def to_sse(self) -> dict[str, str]:
        import json

        return {
            "id": str(self.id),
            "event": self.type,
            "data": json.dumps(
                {
                    "id": self.id,
                    "run_id": self.run_id,
                    "step_id": self.step_id,
                    "type": self.type,
                    "message": self.message,
                    "payload": self.payload,
                    "created_at": self.created_at.isoformat(),
                },
                default=str,
            ),
        }

    @classmethod
    def from_row(cls, row: RunEvent) -> EventRecord:
        created = row.created_at
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=utcnow().tzinfo)
        return cls(
            id=row.id,
            run_id=row.run_id,
            type=row.type,
            message=row.message or "",
            payload=row.payload or {},
            step_id=row.step_id,
            created_at=created or utcnow(),
        )


class EventBus:
    """In-process pub/sub. One instance per API process."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[EventRecord | None]]] = defaultdict(set)
        self._lock = asyncio.Lock()
        # run_id -> asyncio.Event set when the run finishes, so SSE streams can close
        self._closers: dict[str, asyncio.Event] = {}

    # --- subscription ---------------------------------------------------------
    async def subscribe(self, run_id: str) -> asyncio.Queue[EventRecord | None]:
        queue: asyncio.Queue[EventRecord | None] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        async with self._lock:
            self._subscribers[run_id].add(queue)
        return queue

    async def unsubscribe(self, run_id: str, queue: asyncio.Queue[EventRecord | None]) -> None:
        async with self._lock:
            subs = self._subscribers.get(run_id)
            if subs:
                subs.discard(queue)
                if not subs:
                    self._subscribers.pop(run_id, None)

    def subscriber_count(self, run_id: str) -> int:
        return len(self._subscribers.get(run_id, ()))

    def close_channel(self, run_id: str) -> None:
        """Signal subscribers that no further events are coming."""
        event = self._closers.setdefault(run_id, asyncio.Event())
        event.set()
        for queue in list(self._subscribers.get(run_id, ())):
            _put_nowait(queue, None)

    def channel_closed(self, run_id: str) -> asyncio.Event:
        return self._closers.setdefault(run_id, asyncio.Event())

    def reset(self, run_id: str) -> None:
        self._closers.pop(run_id, None)

    # --- publish --------------------------------------------------------------
    async def publish(self, record: EventRecord) -> int:
        delivered = 0
        for queue in list(self._subscribers.get(record.run_id, ())):
            if _put_nowait(queue, record):
                delivered += 1
        return delivered

    def reset_all(self) -> None:
        """Test helper: drop all state."""
        self._subscribers.clear()
        self._closers.clear()


def _put_nowait(queue: asyncio.Queue[EventRecord | None], item: EventRecord | None) -> bool:
    """Best-effort enqueue; drop oldest on overflow instead of blocking the agent."""
    try:
        queue.put_nowait(item)
        return True
    except asyncio.QueueFull:
        try:
            queue.get_nowait()  # discard the oldest frame
            queue.put_nowait(item)
        except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover
            pass
        return False


_bus: EventBus | None = None


def get_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus


def reset_bus() -> None:
    """Test helper."""
    global _bus
    if _bus is not None:
        _bus.reset_all()
    _bus = None


async def append_event(
    session: AsyncSession,
    run_id: str,
    event_type: EventType | str,
    *,
    message: str = "",
    payload: dict[str, Any] | None = None,
    step_id: str | None = None,
    commit: bool = True,
    publish: bool = True,
) -> EventRecord:
    """Persist one audit row and (optionally) stream it to subscribers.

    Returns the flushed record so callers can log/return it immediately.
    """
    etype = event_type.value if isinstance(event_type, EventType) else str(event_type)
    safe_payload = scrub(payload or {})
    row = RunEvent(
        run_id=run_id,
        step_id=step_id,
        type=etype,
        message=message,
        payload=safe_payload,
    )
    session.add(row)
    if commit:
        await session.commit()
    else:
        await session.flush()

    record = EventRecord(
        id=row.id,
        run_id=run_id,
        type=etype,
        message=message,
        payload=safe_payload,
        step_id=step_id,
        created_at=row.created_at or utcnow(),
    )
    log_event(
        logger,
        logging.DEBUG,
        f"event:{etype}",
        run_id=run_id,
        step_id=step_id,
        message=message,
    )
    if publish:
        await get_bus().publish(record)
    return record


async def fetch_events(
    session: AsyncSession,
    run_id: str,
    *,
    after_id: int | None = None,
    limit: int = 500,
    types: list[str] | None = None,
    ascending: bool = True,
) -> list[EventRecord]:
    """Replay stored events (used by SSE resume and the audit page)."""
    stmt = select(RunEvent).where(RunEvent.run_id == run_id)
    if after_id is not None:
        stmt = stmt.where(RunEvent.id > after_id)
    if types:
        stmt = stmt.where(RunEvent.type.in_(types))
    stmt = stmt.order_by(RunEvent.id.asc() if ascending else RunEvent.id.desc()).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    records = [EventRecord.from_row(r) for r in rows]
    return records if ascending else list(reversed(records))


async def stream_run(
    session_factory,
    run_id: str,
    *,
    last_event_id: int | None = None,
    heartbeat: float = HEARTBEAT_SECONDS,
    types: list[str] | None = None,
) -> AsyncIterator[dict[str, str] | None]:
    """Yield SSE frames for a run: replay backlog, then live, then close.

    ``None`` is yielded as a heartbeat tick so the caller can emit a comment
    frame (keeps proxies and browsers from timing out). Streaming stops when the
    run reaches a terminal state *and* its queue is drained.
    """
    bus = get_bus()
    queue = await bus.subscribe(run_id)
    cursor = last_event_id or 0
    try:
        # 1) backlog — everything the client missed, strictly ordered
        async with session_factory() as session:
            backlog = await fetch_events(session, run_id, after_id=cursor, limit=2000)
        for record in backlog:
            if types and record.type not in types:
                continue
            cursor = max(cursor, record.id)
            yield record.to_sse()

        if _is_terminal_payload(backlog):
            return

        # 2) live tail
        closer = bus.channel_closed(run_id)
        while True:
            if closer.is_set() and queue.empty():
                return
            try:
                item = await asyncio.wait_for(queue.get(), timeout=heartbeat)
            except asyncio.TimeoutError:
                yield None  # heartbeat
                continue
            if item is None:
                # flush anything still buffered, then stop
                while not queue.empty():
                    queued = queue.get_nowait()
                    if queued is not None and (not types or queued.type in types):
                        yield queued.to_sse()
                return
            if item.id <= cursor:
                continue  # duplicate protection across replay/live boundary
            cursor = item.id
            if types and item.type not in types:
                continue
            yield item.to_sse()
    finally:
        await bus.unsubscribe(run_id, queue)


def _is_terminal_payload(records: list[EventRecord]) -> bool:
    terminal = {
        EventType.RUN_COMPLETED.value,
        EventType.RUN_FAILED.value,
        EventType.RUN_CANCELLED.value,
    }
    return any(r.type in terminal for r in records)


__all__ = [
    "HEARTBEAT_SECONDS",
    "EventBus",
    "EventRecord",
    "append_event",
    "fetch_events",
    "get_bus",
    "reset_bus",
    "stream_run",
]
