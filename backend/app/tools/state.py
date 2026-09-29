"""Small bookkeeping tools.

These are deliberately *not* clever. Their job is to give the planner a legal way
to (a) park an intermediate fact and (b) declare the goal reached, so it never
invents untracked side channels.
"""

from __future__ import annotations

from typing import Any

from app.core.models import RiskLevel
from app.core.schemas import ToolResult
from app.tools.base import Tool, ToolContext


class NoteTool(Tool):
    """Records a fact in the run's scratchpad."""

    name = "note"
    read_only = True
    description = (
        "Record a short factual note in the run scratchpad (no side effects). "
        "Use it to carry data forward between steps."
    )
    risk_level = RiskLevel.LOW
    category = "memory"
    timeout_seconds = 5

    @property
    def args_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The note to remember"},
                "key": {"type": "string", "description": "Optional label"},
            },
            "required": ["text"],
            "additionalProperties": False,
        }

    @property
    def examples(self) -> list[dict[str, Any]]:
        return [{"text": "Cheapest cable is 'Nylon Braided USB-C 1m' at $4.99", "key": "cheapest"}]

    async def _run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        key = str(args.get("key") or f"note_{len(ctx.state.get('notes', [])) + 1}")
        notes = ctx.state.setdefault("notes", [])
        notes.append({"key": key, "text": str(args["text"])})
        await ctx.notify("observation", f"Noted: {key}", key=key, text=str(args["text"]))
        return ToolResult(
            ok=True,
            summary=f"Stored note {key!r}",
            data={"key": key},
            extracted={key: str(args["text"])},
        )


class TaskCompleteTool(Tool):
    """Declares the user's goal satisfied and carries the final answer."""

    name = "task_complete"
    read_only = True
    description = (
        "Finish the run and return the final answer to the user. Call this as the "
        "LAST step once the goal is achieved. `summary` is shown verbatim on the "
        "completion screen, so make it a complete, self-contained answer."
    )
    risk_level = RiskLevel.LOW
    category = "control"
    timeout_seconds = 5

    @property
    def args_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "The final answer / deliverable for the user",
                },
                "artifacts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Paths or URLs of produced artifacts",
                },
                "success": {"type": "boolean", "default": True},
            },
            "required": ["summary"],
            "additionalProperties": False,
        }

    @property
    def examples(self) -> list[dict[str, Any]]:
        return [{"summary": "Added the cheapest USB-C cable ($4.99) to the cart and stopped at payment."}]

    async def _run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        summary = str(args["summary"])
        artifacts = [str(a) for a in (args.get("artifacts") or [])]
        ctx.state["final_answer"] = summary
        ctx.state["final_artifacts"] = artifacts
        ctx.state["declared_complete"] = bool(args.get("success", True))
        return ToolResult(
            ok=True,
            summary="Goal marked complete",
            data={"final_answer": summary, "artifacts": artifacts},
            extracted={"final_answer": summary},
        )


__all__ = ["NoteTool", "TaskCompleteTool"]
