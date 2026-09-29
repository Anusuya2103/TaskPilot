"""The approval gate's rendezvous point.

The executor must *stop executing* at a risky step until a human decides. In
async Python that means a real blocking point — an :class:`asyncio.Event` that is
only set by an API call — not a boolean flag the UI could forget to honour.

Three properties make this dependable:

* **The executor blocks.** :meth:`ApprovalBroker.request` does not return until a
  decision, a cancellation, or the timeout fires. There is no path that lets a
  gated tool run without a recorded decision.
* **Two wake-up channels.** The in-process :class:`asyncio.Event` is the fast path;
  a database poll runs alongside it so a decision recorded by a *different* worker
  (or a different process behind a load balancer) still unblocks the run.
* **Timeout is a decision.** An unanswered approval is an explicit auto-*reject*,
  written to the audit log with ``timed_out`` — never a silent "assume approved".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger, log_event
from app.core.models import ApprovalStatus

logger = get_logger(__name__)

POLL_INTERVAL_SECONDS = 0.75


@dataclass(slots=True)
class ApprovalOutcome:
    """What the waiting executor learns about the human's decision."""

    status: ApprovalStatus = ApprovalStatus.TIMED_OUT
    args: dict[str, Any] = field(default_factory=dict)
    decided_by: str | None = None
    note: str | None = None
    approval_id: str | None = None

    @property
    def granted(self) -> bool:
        return self.status in {ApprovalStatus.APPROVED, ApprovalStatus.EDITED}

    @property
    def rejected(self) -> bool:
        return not self.granted


@dataclass
class _Pending:
    event: asyncio.Event
    outcome: ApprovalOutcome | None = None


class ApprovalBroker:
    """Coordinates the paused executor and the approving API route."""

    def __init__(self, *, poll_interval: float = POLL_INTERVAL_SECONDS) -> None:
        self._pending: dict[tuple[str, str], _Pending] = {}
        self._lock = asyncio.Lock()
        self.poll_interval = poll_interval

    # --- executor side --------------------------------------------------------
    async def request(
        self,
        *,
        run_id: str,
        step_id: str,
        timeout: float,
        session_factory: Any = None,
    ) -> ApprovalOutcome:
        """Block until a human decides, the run is cancelled, or time runs out."""
        key = (run_id, step_id)
        async with self._lock:
            pending = self._pending.get(key)
            if pending is None:
                pending = _Pending(event=asyncio.Event())
                self._pending[key] = pending
        log_event(
            logger,
            logging.INFO,
            "executor blocked awaiting human approval",
            run_id=run_id,
            step_id=step_id,
            timeout_seconds=timeout,
        )
        return await self._await(key, pending, timeout, session_factory)

    async def _await(
        self,
        key: tuple[str, str],
        pending: _Pending,
        timeout: float,
        session_factory: Any,
    ) -> ApprovalOutcome:
        run_id, step_id = key
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(1.0, timeout)
        try:
            while not pending.event.is_set():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                waiter = asyncio.ensure_future(pending.event.wait())
                poller = (
                    asyncio.ensure_future(self._poll_db(session_factory, run_id, step_id))
                    if session_factory is not None
                    else None
                )
                try:
                    await asyncio.wait(
                        {waiter} | ({poller} if poller else set()),
                        timeout=min(remaining, self.poll_interval),
                    )
                finally:
                    waiter.cancel()
                    if poller is not None:
                        poller.cancel()
                    # Surface a decision the poller found (another worker's write).
                    if poller is not None and poller.done() and not poller.cancelled():
                        with contextlib.suppress(Exception):
                            found = poller.result()
                            if found is not None and pending.outcome is None:
                                pending.outcome = found
                                pending.event.set()

            if pending.outcome is not None:
                return pending.outcome
            outcome = ApprovalOutcome(status=ApprovalStatus.TIMED_OUT)
        finally:
            async with self._lock:
                if self._pending.get(key) is pending:
                    self._pending.pop(key, None)

        log_event(
            logger,
            logging.WARNING,
            "approval timed out -> auto-rejected",
            run_id=run_id,
            step_id=step_id,
        )
        return outcome

    # --- API side -------------------------------------------------------------
    def decide(
        self,
        *,
        run_id: str,
        step_id: str,
        status: ApprovalStatus,
        args: dict[str, Any] | None = None,
        decided_by: str | None = None,
        note: str | None = None,
        approval_id: str | None = None,
    ) -> bool:
        """Wake a blocked executor. Returns False if nothing was waiting."""
        key = (run_id, step_id)
        pending = self._pending.get(key)
        if pending is None:
            log_event(
                logger,
                logging.INFO,
                "approval decision recorded but no executor is waiting",
                run_id=run_id,
                step_id=step_id,
                status=status.value,
            )
            return False
        pending.outcome = ApprovalOutcome(
            status=status,
            args=args or {},
            decided_by=decided_by,
            note=note,
            approval_id=approval_id,
        )
        pending.event.set()
        return True

    def cancel_run(self, run_id: str, *, reason: str = "run cancelled") -> int:
        """Reject every pending approval for a run (used by the cancel endpoint)."""
        keys = [k for k in self._pending if k[0] == run_id]
        for key in keys:
            pending = self._pending[key]
            pending.outcome = ApprovalOutcome(status=ApprovalStatus.CANCELLED, note=reason)
            pending.event.set()
        if keys:
            log_event(logger, logging.INFO, "cancelled pending approvals",
                      run_id=run_id, count=len(keys))
        return len(keys)

    def is_waiting(self, run_id: str, step_id: str | None = None) -> bool:
        if step_id is not None:
            return (run_id, step_id) in self._pending
        return any(k[0] == run_id for k in self._pending)

    def waiting_steps(self, run_id: str) -> list[str]:
        return [k[1] for k in self._pending if k[0] == run_id]

    # --- cross-process safety net --------------------------------------------
    async def _poll_db(
        self, session_factory: Any, run_id: str, step_id: str
    ) -> ApprovalOutcome | None:
        """Look for a decision written by another worker."""
        from sqlalchemy import select

        from app.core.models import Approval

        async with session_factory() as session:
            result = await session.execute(
                select(Approval)
                .where(Approval.run_id == run_id, Approval.step_id == step_id)
                .order_by(Approval.requested_at.desc())
                .limit(1)
            )
            approval = result.scalars().first()
        if approval is None or approval.status == ApprovalStatus.PENDING.value:
            return None
        return ApprovalOutcome(
            status=ApprovalStatus(approval.status),
            args=approval.effective_args,
            decided_by=approval.decided_by,
            note=approval.decision_note,
            approval_id=approval.id,
        )

    def reset(self) -> None:
        """Test helper: unblock and forget everything."""
        for pending in self._pending.values():
            pending.outcome = ApprovalOutcome(status=ApprovalStatus.CANCELLED)
            pending.event.set()
        self._pending.clear()


_broker: ApprovalBroker | None = None


def get_broker() -> ApprovalBroker:
    global _broker
    if _broker is None:
        _broker = ApprovalBroker()
    return _broker


def reset_broker() -> None:
    """Test helper."""
    global _broker
    if _broker is not None:
        _broker.reset()
    _broker = None


__all__ = ["ApprovalBroker", "ApprovalOutcome", "get_broker", "reset_broker"]
