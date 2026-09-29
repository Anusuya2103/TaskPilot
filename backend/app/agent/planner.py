"""The planner: plain-language task → validated, policy-checked step list.

Responsibilities, in order:

1. Render the prompt with the live tool catalogue and run context.
2. Ask the LLM for a :class:`~app.core.schemas.Plan` (JSON, schema-validated,
   auto-repaired by :class:`~app.agent.llm.LLMClient`).
3. **Reject plans that reference tools which do not exist**, substituting a
   close match when the registry can suggest one, and deleting the step when it
   cannot. An unknown tool is a hard failure, never a runtime surprise.
4. Classify every step through :class:`~app.agent.agent.policy.PolicyEngine` and
   persist the per-step risk, reasons and approval requirement *before* the
   executor is allowed to see the plan.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date

from typing import Any

from app.agent.llm import LLMClient
from app.agent.policy import PolicyDecision, PolicyEngine
from app.agent.prompts import (
    CONTEXT_BLOCK,
    PLANNER_SYSTEM,
    PLANNER_USER,
    REPLANNER_SYSTEM,
    REPLANNER_USER,
    render,
)
from app.core.config import Settings, get_settings
from app.core.errors import LLMError
from app.core.logging import get_logger, log_event
from app.core.models import RiskLevel
from app.core.schemas import Plan, PlanStep
from app.core.security import sanitize_untrusted
from app.tools.base import Tool
from app.tools.registry import ToolRegistry, get_registry

logger = get_logger(__name__)


@dataclass
class PlannedStep:
    """A plan step plus the policy verdict attached at plan time."""

    step: PlanStep
    index: int
    decision: PolicyDecision
    tool: Tool | None = None
    status: str = "pending"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.step.id,
            "index": self.index,
            "goal": self.step.goal,
            "tool": self.step.tool,
            "args": self.step.args,
            "expected_outcome": self.step.expected_outcome,
            "risk": self.step.risk,
            "effective_risk": self.decision.effective_risk.value,
            "needs_approval": self.decision.needs_approval,
            "risk_reasons": [r.to_dict() for r in self.decision.reasons],
            "status": self.status,
            "error": self.error,
        }


@dataclass
class PlanResult:
    plan: Plan
    steps: list[PlannedStep] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    provider: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def requires_approval(self) -> bool:
        return any(s.decision.needs_approval for s in self.steps)

    @property
    def highest_risk(self) -> RiskLevel:
        return RiskLevel.max_of(*[s.decision.effective_risk for s in self.steps])

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.plan.model_dump(),
            "steps": [s.to_dict() for s in self.steps],
            "warnings": self.warnings,
            "requires_approval": self.requires_approval,
            "highest_risk": self.highest_risk.value,
        }


class Planner:
    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.llm = llm
        self.registry = registry or get_registry()
        self.settings = settings or get_settings()
        self.policy = PolicyEngine(self.settings)

    # --- initial plan ---------------------------------------------------------
    async def create_plan(
        self,
        goal: str,
        *,
        context: str = "",
        max_steps: int | None = None,
    ) -> PlanResult:
        started = time.perf_counter()
        prompt = render(
            PLANNER_USER,
            goal=sanitize_untrusted(goal, max_chars=2000, keep_suspicious=True).text,
            mockshop_url=self.settings.mockshop_base_url,
            books_url=self.settings.books_base_url,
            today=date.today().isoformat(),
            allowlist=", ".join(self.settings.allowlist_patterns[:12]) or "*",
            catalogue=self.registry.prompt_catalog() or "(no tools registered)",
            context_block=(
                render(CONTEXT_BLOCK, context=sanitize_untrusted(context, max_chars=4000).text)
                if context
                else ""
            ),
        )
        plan = await self.llm.complete_structured(
            Plan, prompt, system=PLANNER_SYSTEM, max_repairs=2
        )
        return self.finalise(plan, started=started, max_steps=max_steps)

    # --- replan ---------------------------------------------------------------
    async def replan(
        self,
        *,
        goal: str,
        completed: list[PlanStep],
        failed: PlanStep | None,
        failure_reason: str,
        remaining: list[PlanStep],
        last_observation: str = "",
    ) -> PlanResult:
        started = time.perf_counter()
        prompt = render(
            REPLANNER_USER,
            goal=goal,
            completed="\n".join(f"- {s.id}: {s.goal}" for s in completed) or "(none)",
            failed=f"{failed.id}: {failed.goal}" if failed else "(none)",
            failure_reason=failure_reason or "(unspecified)",
            remaining="\n".join(f"- {s.id}: {s.goal}" for s in remaining) or "(none)",
            available_tools=self.registry.prompt_catalog(),
            observation_block=(
                sanitize_untrusted(last_observation, max_chars=4000).as_prompt_block(
                    "last_observation"
                )
                + "\n\n"
                if last_observation
                else ""
            ),
        )
        plan = await self.llm.complete_structured(
            Plan, prompt, system=REPLANNER_SYSTEM, max_repairs=2
        )
        return self.finalise(plan, started=started)

    # --- validation + policy --------------------------------------------------
    def finalise(
        self, plan: Plan, *, started: float, max_steps: int | None = None
    ) -> PlanResult:
        """Validate tool references, cap the plan size, and classify every step."""
        warnings: list[str] = []
        limit = max_steps or self.settings.max_steps

        kept: list[PlanStep] = []
        for step in plan.steps:
            if len(kept) >= limit:
                warnings.append(
                    f"plan truncated to {limit} steps (dropped: {step.id} {step.goal!r})"
                )
                break
            if not self.registry.has(step.tool):
                suggestions = self.registry.suggest(step.tool)
                if suggestions and suggestions[0] != step.tool:
                    warnings.append(
                        f"step {step.id}: unknown tool {step.tool!r} replaced with "
                        f"{suggestions[0]!r}"
                    )
                    log_event(
                        logger,
                        logging.WARNING,
                        "planner produced an unknown tool; using the closest match",
                        step_id=step.id,
                        requested=step.tool,
                        replacement=suggestions[0],
                    )
                    step.tool = suggestions[0]
                else:
                    warnings.append(
                        f"step {step.id}: dropped, unknown tool {step.tool!r}"
                    )
                    log_event(
                        logger,
                        logging.WARNING,
                        "planner produced an unknown tool; dropping the step",
                        step_id=step.id,
                        requested=step.tool,
                    )
                    continue
            kept.append(step)

        if not kept:
            raise LLMError(
                "the planner returned no usable steps — every step referenced an "
                "unknown tool. Try rephrasing the task or check the available tools.",
                details={"warnings": warnings},
            )

        plan.steps = kept
        steps: list[PlannedStep] = []
        for index, step in enumerate(plan.steps):
            tool = self.registry.get(step.tool)
            decision = self.policy.classify(step, tool, settings=self.settings)
            planned = PlannedStep(step=step, index=index, decision=decision, tool=tool)
            if decision.needs_approval:
                log_event(
                    logger,
                    logging.INFO,
                    "step requires human approval",
                    step_id=step.id,
                    tool=step.tool,
                    risk=decision.effective_risk.value,
                    reasons=decision.explanation()[:300],
                )
            steps.append(planned)

        return PlanResult(
            plan=plan,
            steps=steps,
            warnings=warnings,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            provider=self.llm.provider,
            model=getattr(self.llm, "model", ""),
            tokens_in=int(self.llm.stats.get("tokens_in", 0)),
            tokens_out=int(self.llm.stats.get("tokens_out", 0)),
        )

    # --- plan editing (plan-preview mode) ------------------------------------
    @staticmethod
    def apply_edit(
        plan: Plan,
        edits: list[dict[str, Any]],
        *,
        registry: ToolRegistry | None = None,
    ) -> list[str]:
        """Apply a human's plan edits. Returns a list of applied-edit descriptions.

        Supports editing a step's goal/args/risk or dropping it entirely. This is
        what the "review the plan before you run it" mode writes back.
        """
        registry = registry or get_registry()
        applied: list[str] = []
        by_id = {s.id: s for s in plan.steps}
        for edit in edits or []:
            step = by_id.get(str(edit.get("id")))
            if step is None:
                applied.append(f"ignored edit for unknown step {edit.get('id')!r}")
                continue
            if edit.get("remove") is True:
                plan.steps = [s for s in plan.steps if s.id != step.id]
                by_id = {s.id: s for s in plan.steps}
                applied.append(f"removed step {step.id}")
                continue
            if "args" in edit and isinstance(edit["args"], dict):
                step.args = {**step.args, **edit["args"]}
                applied.append(f"updated args for {step.id}")
            if "goal" in edit and isinstance(edit["goal"], str):
                step.goal = edit["goal"]
                applied.append(f"updated goal for {step.id}")
            if "tool" in edit and isinstance(edit["tool"], str) and registry.has(edit["tool"]):
                step.tool = edit["tool"]
                applied.append(f"swapped tool for {step.id} -> {edit['tool']}")
        plan.steps = [s for s in plan.steps]
        return applied


__all__ = ["PlanResult", "PlannedStep", "Planner"]
