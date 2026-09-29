"""Pydantic v2 schemas: API contracts, LLM I/O contracts, and tool payloads.

The LLM-facing models (:class:`Plan`, :class:`VerificationVerdict`, ...) double as
the *repair targets*: when a model returns invalid JSON we re-prompt it with the
validation error and these schemas as the spec.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.models import (
    ApprovalStatus,
    RiskLevel,
    RunStatus,
    StepStatus,
)

# ---------------------------------------------------------------------------
# shared
# ---------------------------------------------------------------------------
RiskLiteral = Literal["low", "medium", "high"]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ToolCall(BaseModel):
    """What the executor hands to a tool."""

    model_config = ConfigDict(extra="allow")

    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    step_id: str | None = None
    timeout_seconds: float | None = None
    idempotency_key: str | None = None


class ToolResult(BaseModel):
    """Uniform tool output so the verifier/reporter never special-case a tool."""

    ok: bool
    summary: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    extracted: dict[str, Any] = Field(default_factory=dict)
    screenshots: list[str] = Field(default_factory=list)
    url: str | None = None
    latency_ms: float | None = None
    needs_attention: bool = False
    error: str | None = None
    reasons: list[str] = Field(default_factory=list)
    untrusted_content: bool = False


class Observation(BaseModel):
    """Post-step record stored on the step and shown in the UI."""

    summary: str = ""
    ok: bool = True
    data: dict[str, Any] = Field(default_factory=dict)
    extracted: dict[str, Any] = Field(default_factory=dict)
    screenshots: list[str] = Field(default_factory=list)
    url: str | None = None
    latency_ms: float | None = None
    error: str | None = None
    security_flags: list[str] = Field(default_factory=list)
    truncated: bool = False


# ---------------------------------------------------------------------------
# LLM structured-output contracts
# ---------------------------------------------------------------------------
class PlanStep(BaseModel):
    """One ordered action. ``args`` is free-form because tools differ."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default="", description="short stable id, e.g. s1")
    goal: str = Field(min_length=3, description="imperative, one sentence")
    tool: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)
    expected_outcome: str = Field(
        default="", description="observable, checkable completion condition"
    )
    risk: RiskLiteral = "low"
    needs_approval: bool = False
    rationale: str = ""

    @field_validator("id", mode="before")
    @classmethod
    def _id_str(cls, v: Any) -> str:
        return "" if v is None else str(v)

    @model_validator(mode="after")
    def _fill_id(self) -> PlanStep:
        if not self.id:
            # Planner normalises to s1..sN after parse; keep schema tolerant.
            self.id = "s?"
        return self


class Plan(BaseModel):
    """The planner's full output."""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=3)
    steps: list[PlanStep] = Field(min_length=1)
    success_criteria: list[str] = Field(default_factory=list)
    notes: str = ""
    estimated_manual_minutes: float | None = Field(
        default=None, description="human time to do this by hand, for the baseline column"
    )
    requires_approval: bool = False

    @model_validator(mode="after")
    def _normalise_ids(self) -> Plan:
        seen: set[str] = set()
        for i, step in enumerate(self.steps, start=1):
            candidate = step.id if step.id and step.id != "s?" else f"s{i}"
            if candidate in seen:
                candidate = f"s{i}"
            seen.add(candidate)
            step.id = candidate
        return self

    @property
    def highest_risk(self) -> RiskLevel:
        return RiskLevel.max_of(*[s.risk for s in self.steps])


class VerificationVerdict(BaseModel):
    """The verifier's judgement of a single step."""

    model_config = ConfigDict(extra="forbid")

    success: bool
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    reason: str = ""
    missing: list[str] = Field(default_factory=list)
    suggested_retry_args: dict[str, Any] | None = None


class ReplanRequest(BaseModel):
    """Body the executor sends when it needs a fresh plan for the remainder."""

    model_config = ConfigDict(extra="forbid")

    goal: str
    completed_steps: list[str] = Field(default_factory=list)
    failed_step: str = ""
    failure_reason: str = ""
    remaining_steps: list[str] = Field(default_factory=list)
    available_tools: list[str] = Field(default_factory=list)

    @classmethod
    def from_plan(
        cls,
        plan: Plan,
        *,
        completed: list[PlanStep],
        failed: PlanStep | None,
        reason: str,
        remaining: list[PlanStep],
        available_tools: list[str],
    ) -> ReplanRequest:
        return cls(
            goal=plan.goal,
            completed_steps=[f"{s.id}: {s.goal}" for s in completed],
            failed_step="" if failed is None else f"{failed.id}: {failed.goal}",
            failure_reason=reason,
            remaining_steps=[f"{s.id}: {s.goal}" for s in remaining],
            available_tools=available_tools,
        )


class RiskAssessment(BaseModel):
    """Optional second-opinion LLM call used by the policy engine."""

    model_config = ConfigDict(extra="forbid")

    risk: RiskLiteral = "low"
    reasons: list[str] = Field(default_factory=list)
    irreversible: bool = False


# ---------------------------------------------------------------------------
# API: tasks & runs
# ---------------------------------------------------------------------------
class TaskCreate(BaseModel):
    prompt: str = Field(min_length=3, max_length=8000)
    title: str = Field(default="", max_length=255)
    source: str = "ui"
    tags: dict[str, Any] = Field(default_factory=dict)
    # execution options
    auto_start: bool = True
    review_plan: bool = False
    provider: str | None = None
    model: str | None = None
    auto_approve_low_risk: bool | None = None
    max_steps: int | None = Field(default=None, ge=1, le=50)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("prompt")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("prompt must not be blank")
        return v


class TaskOut(ORMModel):
    id: str
    prompt: str
    title: str
    source: str
    created_at: datetime
    tags: dict[str, Any] = Field(default_factory=dict)


class StepOut(ORMModel):
    id: str
    run_id: str
    index: int
    goal: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    expected_outcome: str = ""
    risk: str = RiskLevel.LOW.value
    declared_risk: str = RiskLevel.LOW.value
    effective_risk: str = RiskLevel.LOW.value
    needs_approval: bool = False
    risk_reasons: list[Any] = Field(default_factory=list)
    status: str = StepStatus.PENDING.value
    attempts: int = 0
    observation: dict[str, Any] | None = None
    extracted: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    latency_ms: float | None = None
    approved_at: datetime | None = None


class ScreenshotOut(ORMModel):
    id: str
    step_id: str | None = None
    label: str = ""
    url: str | None = None
    page_title: str | None = None
    sequence: int = 0
    created_at: datetime
    # API-relative or absolute URL the browser can <img src> directly
    src: str = ""


class ApprovalOut(ORMModel):
    id: str
    run_id: str
    step_id: str
    status: str = ApprovalStatus.PENDING.value
    reason: str = ""
    risk: str = RiskLevel.HIGH.value
    risk_reasons: list[Any] = Field(default_factory=list)
    preview: dict[str, Any] = Field(default_factory=dict)
    original_args: dict[str, Any] = Field(default_factory=dict)
    edited_args: dict[str, Any] | None = None
    screenshot_url: str | None = None
    requested_at: datetime
    expires_at: datetime | None = None
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_note: str | None = None
    seconds_remaining: float | None = None


class ApprovalDecision(BaseModel):
    """Body for POST /runs/{id}/approvals/{step_id}."""

    decision: Literal["approve", "reject", "edit_args"]
    edited_args: dict[str, Any] | None = None
    note: str | None = Field(default=None, max_length=2000)
    decided_by: str = Field(default="human", max_length=128)
    resume: bool = True

    @model_validator(mode="after")
    def _check_args(self) -> ApprovalDecision:
        if self.decision == "edit_args" and not self.edited_args:
            raise ValueError("edited_args is required when decision=edit_args")
        if self.decision == "approve" and self.edited_args:
            raise ValueError("use decision=edit_args when supplying edited_args")
        return self


class RunOut(ORMModel):
    id: str
    task_id: str
    status: str = RunStatus.PENDING.value
    goal: str = ""
    plan: dict[str, Any] | None = None
    plan_revision: int = 0
    replan_count: int = 0
    provider: str = ""
    model: str = ""
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: float | None = None
    manual_baseline_seconds: float | None = None
    steps_total: int = 0
    steps_succeeded: int = 0
    approvals_count: int = 0
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    cancel_requested: bool = False
    steps: list[StepOut] = Field(default_factory=list)
    pending_approval: ApprovalOut | None = None
    prompt: str = ""


class RunListOut(ORMModel):
    id: str
    task_id: str
    status: str
    goal: str = ""
    prompt: str = ""
    created_at: datetime
    finished_at: datetime | None = None
    duration_ms: float | None = None
    steps_total: int = 0
    steps_succeeded: int = 0
    approvals_count: int = 0
    error: dict[str, Any] | None = None
    manual_baseline_seconds: float | None = None


class Paginated(BaseModel):
    items: list[Any]
    total: int
    limit: int
    offset: int


class RunEventOut(BaseModel):
    id: int
    run_id: str
    step_id: str | None = None
    type: str
    message: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class CancelResponse(BaseModel):
    run_id: str
    status: str
    accepted: bool
    message: str = ""


# ---------------------------------------------------------------------------
# API: tools, health, settings, reports
# ---------------------------------------------------------------------------
class ToolOut(BaseModel):
    name: str
    description: str = ""
    source: str = "builtin"  # builtin | mcp
    risk_level: RiskLiteral = "low"
    category: str = "general"
    available: bool = True
    server: str | None = None
    args_schema: dict[str, Any] = Field(default_factory=dict)
    examples: list[dict[str, Any]] = Field(default_factory=list)


class ToolListOut(BaseModel):
    tools: list[ToolOut]
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list)


class ProviderInfo(BaseModel):
    id: str
    label: str
    default_model: str
    configured: bool
    models: list[str] = Field(default_factory=list)
    active: bool = False


class HealthOut(BaseModel):
    status: Literal["ok", "degraded", "error"]
    version: str
    env: str
    uptime_seconds: float
    checks: dict[str, Any] = Field(default_factory=dict)


class RuntimeSettings(BaseModel):
    """Read/write view of the settings page.

    ``llm_provider`` and ``llm_model`` are validated against the provider list;
    the high-risk approval requirement is intentionally absent — it is not
    configurable by design.
    """

    llm_provider: Literal["anthropic", "openai", "gemini", "scripted"] | None = None
    llm_model: str | None = None
    llm_temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    mcp_enabled: bool | None = None
    mcp_servers: list[dict[str, Any]] | None = None
    domain_allowlist: list[str] | None = None
    auto_approve_low_risk: bool | None = None
    approval_timeout_seconds: int | None = Field(default=None, ge=10, le=3600)
    max_step_attempts: int | None = Field(default=None, ge=1, le=5)
    max_replans: int | None = Field(default=None, ge=0, le=5)
    browser_headless: bool | None = None
    planner_preview: bool | None = None

    def touched(self) -> dict[str, Any]:
        return {k: v for k, v in self.model_dump().items() if v is not None}


class SettingsOut(BaseModel):
    settings: dict[str, Any]
    providers: list[ProviderInfo] = Field(default_factory=list)
    tool_count: int = 0
    high_risk_always_requires_approval: bool = True


class DemoTaskOut(BaseModel):
    id: str
    title: str
    prompt: str
    needs_approval: bool
    tools: list[str] = Field(default_factory=list)
    estimated_manual_minutes: float = 2.0
    category: str = "demo"
    #: One-line explanation shown as the chip's tooltip in the UI.
    description: str = ""


class ReportOut(BaseModel):
    run_id: str
    markdown: str
    json_report: dict[str, Any] = Field(default_factory=dict)
    download_urls: dict[str, str] = Field(default_factory=dict)


class RunSummaryOut(BaseModel):
    run_id: str
    status: str
    goal: str = ""
    output: str = ""
    steps_total: int = 0
    steps_succeeded: int = 0
    steps_failed: int = 0
    steps_skipped: int = 0
    step_accuracy: float = 0.0
    steps_requiring_approval: int = 0
    approvals_requested: int = 0
    approvals_granted: int = 0
    approvals_rejected: int = 0
    approvals_timed_out: int = 0
    risky_actions_executed_without_approval: int = 0
    duration_seconds: float = 0.0
    manual_baseline_seconds: float | None = None
    time_saved_seconds: float | None = None
    extracted: dict[str, Any] = Field(default_factory=dict)
    screenshots: list[str] = Field(default_factory=list)
    replans: int = 0
    llm_calls: int = 0


class ErrorOut(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


__all__ = [
    "ApprovalDecision",
    "ApprovalOut",
    "CancelResponse",
    "DemoTaskOut",
    "ErrorOut",
    "HealthOut",
    "Observation",
    "Paginated",
    "Plan",
    "PlanStep",
    "ProviderInfo",
    "ReplanRequest",
    "ReportOut",
    "RiskAssessment",
    "RunEventOut",
    "RunListOut",
    "RunOut",
    "RunSummaryOut",
    "RuntimeSettings",
    "ScreenshotOut",
    "SettingsOut",
    "StepOut",
    "TaskCreate",
    "TaskOut",
    "ToolCall",
    "ToolListOut",
    "ToolOut",
    "ToolResult",
    "VerificationVerdict",
]
