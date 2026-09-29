"""Domain exceptions and the mapping to HTTP status codes.

Every error the agent can surface carries (a) a machine code, (b) a human
message suitable for the UI, and (c) a `retryable` hint the UI uses to decide
whether to offer "retry" or "edit plan".
"""

from __future__ import annotations

from typing import Any


class TaskPilotError(Exception):
    """Base class. `code` is stable and safe to assert on in tests."""

    code: str = "taskpilot_error"
    http_status: int = 500
    retryable: bool = False

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


class NotFoundError(TaskPilotError):
    code = "not_found"
    http_status = 404


class ValidationFailure(TaskPilotError):
    code = "validation_error"
    http_status = 422


class ConflictError(TaskPilotError):
    """e.g. approving a step that is no longer awaiting approval."""

    code = "conflict"
    http_status = 409
    retryable = True


class ToolError(TaskPilotError):
    """A tool ran but failed. Usually retryable (transient network / UI race)."""

    code = "tool_error"
    http_status = 502
    retryable = True


class ToolNotFoundError(TaskPilotError):
    code = "tool_not_found"
    http_status = 400


class ToolTimeoutError(ToolError):
    code = "tool_timeout"


class PolicyViolation(TaskPilotError):
    """Blocked by the domain allowlist or another hard safety rule."""

    code = "policy_violation"
    http_status = 403


class ApprovalRequired(TaskPilotError):
    """Raised when an action is attempted without a recorded human approval.

    This is the enforcement arm of the approval gate: it must be raised from the
    executor, never from the UI, so no code path can bypass it.
    """

    code = "approval_required"
    http_status = 409
    retryable = True


class ApprovalRejected(ApprovalRequired):
    code = "approval_rejected"


class ApprovalTimeout(ApprovalRequired):
    code = "approval_timeout"


class RunCancelled(TaskPilotError):
    code = "run_cancelled"
    http_status = 409


class LLMError(TaskPilotError):
    code = "llm_error"
    http_status = 502
    retryable = True


class LLMOutputError(LLMError):
    """Model returned content we could not repair into the requested schema."""

    code = "llm_invalid_output"


class BudgetExceeded(TaskPilotError):
    code = "budget_exceeded"
    http_status = 429

    def __init__(self, message: str, *, limit: int, used: int) -> None:
        super().__init__(message, details={"limit": limit, "used": used})
        self.limit = limit
        self.used = used


class RateLimited(TaskPilotError):
    code = "rate_limited"
    http_status = 429
    retryable = True


class UnsafeContentDetected(TaskPilotError):
    """Prompt-injection style content found where it was not expected."""

    code = "unsafe_content"
    http_status = 400


__all__ = [
    "ApprovalRejected",
    "ApprovalRequired",
    "ApprovalTimeout",
    "BudgetExceeded",
    "ConflictError",
    "LLMError",
    "LLMOutputError",
    "NotFoundError",
    "PolicyViolation",
    "RateLimited",
    "RunCancelled",
    "TaskPilotError",
    "ToolError",
    "ToolNotFoundError",
    "ToolTimeoutError",
    "UnsafeContentDetected",
    "ValidationFailure",
]
