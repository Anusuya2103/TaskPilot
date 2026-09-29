"""Database models.

Design notes
------------
* Enum-ish fields are stored as plain strings with Python-side ``str`` enums.
  This keeps migrations trivial and identical on SQLite and Postgres.
* ``Event.id`` is a monotonic integer because it doubles as the SSE
  ``Last-Event-ID`` cursor — resumability is a schema-level guarantee, not a
  runtime hack.
* ``Step.idempotency_key`` makes retries safe: a step that already completed
  with the same key is never executed twice.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    """Declarative base. ``type_annotation_map`` keeps JSON portable."""

    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


# ---------------------------------------------------------------------------
# enums (string-valued so they round-trip through SQLite/Postgres untouched)
# ---------------------------------------------------------------------------
class RunStatus(str, enum.Enum):
    PENDING = "pending"
    PLANNING = "planning"
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @classmethod
    def terminal(cls) -> set[RunStatus]:
        return {cls.COMPLETED, cls.FAILED, cls.CANCELLED}

    def is_terminal(self) -> bool:
        return self in self.terminal()

    def is_paused(self) -> bool:
        return self in {RunStatus.AWAITING_APPROVAL, RunStatus.AWAITING_PLAN_APPROVAL}


class StepStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"          # waiting on a human
    AWAITING_APPROVAL = "awaiting_approval"

    def is_terminal(self) -> bool:
        return self in {StepStatus.DONE, StepStatus.FAILED, StepStatus.SKIPPED}


class RiskLevel(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return {"low": 0, "medium": 1, "high": 2}[self.value]

    @classmethod
    def max_of(cls, *levels: RiskLevel | str | None) -> RiskLevel:
        """Policy uses this: effective risk = max(rule risk, LLM risk)."""
        best = cls.LOW
        for lvl in levels:
            if lvl is None:
                continue
            cand = cls(lvl) if isinstance(lvl, str) else lvl
            if cand.rank > best.rank:
                best = cand
        return best


class ApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EDITED = "edited"            # approved with modified args
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"

    @property
    def granted(self) -> bool:
        return self in {ApprovalStatus.APPROVED, ApprovalStatus.EDITED}


class EventType(str, enum.Enum):
    RUN_CREATED = "run_created"
    RUN_STARTED = "run_started"
    PLAN_CREATED = "plan_created"
    PLAN_REVISED = "plan_revised"
    PLAN_APPROVED = "plan_approved"
    PLAN_REJECTED = "plan_rejected"
    STEP_STARTED = "step_started"
    TOOL_CALL = "tool_call"
    OBSERVATION = "observation"
    SCREENSHOT = "screenshot"
    VERIFICATION = "verification"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_DECISION = "approval_decision"
    STEP_COMPLETED = "step_completed"
    STEP_FAILED = "step_failed"
    STEP_SKIPPED = "step_skipped"
    RETRY = "retry"
    REPLAN = "replan"
    POLICY_DECISION = "policy_decision"
    SECURITY_NOTICE = "security_notice"
    COST = "cost"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"
    LOG = "log"


# ---------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------
class Task(Base):
    """The raw, plain-language request a human typed."""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source: Mapped[str] = mapped_column(String(32), default="ui")  # ui|api|cli|eval
    tags: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    runs: Mapped[list["Run"]] = relationship(
        back_populates="task", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (Index("ix_tasks_created_at", "created_at"),)


class Run(Base):
    """One execution of a task: plan + steps + events + approvals."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), default=RunStatus.PENDING.value, index=True)
    goal: Mapped[str] = mapped_column(Text, default="")

    # planning artefacts
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    plan_revision: Mapped[int] = mapped_column(Integer, default=0)
    replan_count: Mapped[int] = mapped_column(Integer, default=0)

    # execution bookkeeping
    provider: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)

    # metrics
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    manual_baseline_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    steps_total: Mapped[int] = mapped_column(Integer, default=0)
    steps_succeeded: Mapped[int] = mapped_column(Integer, default=0)
    approvals_count: Mapped[int] = mapped_column(Integer, default=0)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    llm_tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    llm_tokens_out: Mapped[int] = mapped_column(Integer, default=0)

    task: Mapped[Task] = relationship(back_populates="runs", lazy="selectin")
    steps: Mapped[list["Step"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="Step.index",
    )
    approvals: Mapped[list["Approval"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="selectin"
    )
    screenshots: Mapped[list["Screenshot"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        Index("ix_runs_status_created", "status", "created_at"),
        Index("ix_runs_task_created", "task_id", "created_at"),
    )

    @property
    def is_terminal(self) -> bool:
        return self.status in {s.value for s in RunStatus.terminal()}

    @property
    def is_paused(self) -> bool:
        return self.status in {
            RunStatus.AWAITING_APPROVAL.value,
            RunStatus.AWAITING_PLAN_APPROVAL.value,
        }


class Step(Base):
    """A single ordered action in a plan, plus its execution record."""

    __tablename__ = "steps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    index: Mapped[int] = mapped_column(Integer, default=0)

    goal: Mapped[str] = mapped_column(Text, default="")
    tool: Mapped[str] = mapped_column(String(128), default="")
    args: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    expected_outcome: Mapped[str] = mapped_column(Text, default="")

    risk: Mapped[str] = mapped_column(String(16), default=RiskLevel.LOW.value)
    declared_risk: Mapped[str] = mapped_column(String(16), default=RiskLevel.LOW.value)
    effective_risk: Mapped[str] = mapped_column(String(16), default=RiskLevel.LOW.value)
    needs_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_reasons: Mapped[list[Any]] = mapped_column(JSON, default=list)

    status: Mapped[str] = mapped_column(String(24), default=StepStatus.PENDING.value, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(128), default="", index=True)

    observation: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    extracted: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    verification: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[Run] = relationship(back_populates="steps")
    approvals: Mapped[list["Approval"]] = relationship(
        back_populates="step", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        UniqueConstraint("run_id", "index", name="uq_step_run_index"),
        Index("ix_steps_run_status", "run_id", "status"),
    )


class Approval(Base):
    """A recorded human decision. Immutable once written (status changes once)."""

    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    step_id: Mapped[str] = mapped_column(ForeignKey("steps.id", ondelete="CASCADE"), index=True)

    status: Mapped[str] = mapped_column(String(24), default=ApprovalStatus.PENDING.value)
    reason: Mapped[str] = mapped_column(Text, default="")
    risk: Mapped[str] = mapped_column(String(16), default=RiskLevel.HIGH.value)
    risk_reasons: Mapped[list[Any]] = mapped_column(JSON, default=list)

    # what the human is being asked to authorise
    preview: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    original_args: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    edited_args: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    screenshot_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    channel: Mapped[str] = mapped_column(String(32), default="ui")

    run: Mapped[Run] = relationship(back_populates="approvals")
    step: Mapped[Step] = relationship(back_populates="approvals")

    @property
    def effective_args(self) -> dict[str, Any]:
        """Approved args if the human edited them, otherwise the original."""
        return dict(self.edited_args) if self.edited_args else dict(self.original_args)

    @property
    def granted(self) -> bool:
        return self.status in {ApprovalStatus.APPROVED.value, ApprovalStatus.EDITED.value}


class RunEvent(Base):
    """Append-only audit log.

    ``id`` is the SSE cursor: clients resume with ``Last-Event-ID`` and get
    exactly the events they missed, in order, with no duplicates.
    """

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    step_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(48), index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    __table_args__ = (Index("ix_events_run_id_id", "run_id", "id"),)


class Screenshot(Base):
    """A PNG captured after every browser action (the visual audit trail)."""

    __tablename__ = "screenshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    step_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    label: Mapped[str] = mapped_column(String(255), default="")
    path: Mapped[str] = mapped_column(String(512), default="")
    url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    page_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[Run] = relationship(back_populates="screenshots")

    __table_args__ = (Index("ix_screenshots_run_seq", "run_id", "sequence"),)


class Setting(Base):
    """Persisted, UI-editable overrides (LLM provider, MCP servers, allowlist).

    Deliberately a key/value table: settings change shape often, and a migration
    per new toggle would be noise. Values are validated by ``schemas.RuntimeSettings``.
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class StepIdempotency(Base):
    """Guards against duplicate execution of the same logical step."""

    __tablename__ = "step_idempotency"

    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    step_index: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


__all__ = [
    "Approval",
    "ApprovalStatus",
    "Base",
    "EventType",
    "RiskLevel",
    "Run",
    "RunEvent",
    "RunStatus",
    "Screenshot",
    "Setting",
    "Step",
    "StepIdempotency",
    "StepStatus",
    "Task",
    "new_id",
    "utcnow",
]
