"""The verifier: did the step actually do what it was supposed to do?

Two layers, in this order:

1. **Deterministic checks** (always run, no LLM, no cost, no flakiness).
   A failed tool result, an empty cart after "add to cart", a page that still
   shows a validation error — all of these are decidable with string and
   structure checks, and deciding them locally makes the agent fast and cheap.
2. **LLM judgement** (only when layer 1 is inconclusive and an LLM is available).
   Reserved for fuzzy goals like "the report looks complete".

Because layer 1 is authoritative for failure, a run can never be marked successful
just because a model was polite about it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger
from app.core.schemas import PlanStep, VerificationVerdict
from app.core.security import sanitize_untrusted

logger = get_logger(__name__)

# Markers that a page reported an error even though HTTP was 200.
STRONG_ERROR_MARKERS = (
    "internal server error", "traceback (most recent call last)", "404 not found",
    "access denied", "permission denied", "unauthorized", "forbidden",
    "something went wrong", "unexpected error",
)
WEAK_ERROR_MARKERS = (
    "validation error", "error:", "failed to", "invalid input", "not found",
)
ERROR_FALSE_POSITIVES = (
    "no errors", "0 errors", "error-free", "error handler", "with no error",
)

NUMBER = re.compile(r"-?\d[\d,]*\.?\d*")
MONEY = re.compile(r"\$\s?\d[\d,]*(?:\.\d{1,2})?")
COUNT_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


@dataclass
class DeterministicVerdict:
    decided: bool
    success: bool
    reason: str = ""
    missing: list[str] | None = None


class Verifier:
    """Cheap-first step verification."""

    def __init__(self, llm: Any | None = None) -> None:
        self.llm = llm

    async def verify(
        self,
        step: PlanStep,
        observation: dict[str, Any],
        *,
        tool_ok: bool,
        use_llm: bool = True,
    ) -> VerificationVerdict:
        deterministic = self.check(step, observation, tool_ok=tool_ok)
        if deterministic.decided:
            return VerificationVerdict(
                success=deterministic.success,
                confidence=0.95 if deterministic.success else 0.85,
                reason=deterministic.reason,
                missing=deterministic.missing or [],
            )
        if not use_llm or self.llm is None:
            # Default-trust when we cannot decide: the tool reported ok and no
            # contradiction was found. Recorded with lower confidence so the UI
            # can show it as "assumed".
            return VerificationVerdict(
                success=tool_ok,
                confidence=0.45,
                reason=(
                    "no deterministic contradiction found; trusting the tool result"
                    if tool_ok
                    else "tool reported failure"
                ),
            )
        return await self._llm_verify(step, observation, tool_ok=tool_ok)

    # --- layer 1: deterministic ----------------------------------------------
    def check(
        self, step: PlanStep, observation: dict[str, Any], *, tool_ok: bool
    ) -> DeterministicVerdict:
        text = self._observation_text(observation)

        if not tool_ok:
            error = str(observation.get("error") or observation.get("summary") or "")
            return DeterministicVerdict(
                decided=True,
                success=False,
                reason=f"tool reported failure: {error[:300] or 'unknown error'}",
            )

        if step.tool.startswith("browser_") and observation.get("status") is not None:
            status = int(observation.get("status") or 0)
            if status >= 400:
                return DeterministicVerdict(
                    decided=True, success=False, reason=f"page returned HTTP {status}"
                )

        if self._looks_like_error_page(text):
            return DeterministicVerdict(
                decided=True,
                success=False,
                reason="page contains an error message, so the step did not achieve its goal",
            )

        # If the expected outcome names a price or a count, require it to appear.
        expected = step.expected_outcome or ""
        missing = [
            amount for amount in MONEY.findall(expected) if not self._money_close(amount, text)
        ]
        if missing:
            return DeterministicVerdict(
                decided=True,
                success=False,
                reason=f"expected value(s) not present in the result: {', '.join(missing)}",
                missing=missing,
            )

        for word, value in COUNT_WORDS.items():
            if re.search(rf"\b{word}\b", expected, re.IGNORECASE):
                hint = self._count_hint(observation, value)
                if hint is False:
                    return DeterministicVerdict(
                        decided=True,
                        success=False,
                        reason=f"expected {word} item(s) but the result shows a different count",
                        missing=[word],
                    )

        if self._expects_item(step) and self._is_empty_result(observation):
            return DeterministicVerdict(
                decided=True,
                success=False,
                reason="the result is empty but the step expected content",
            )

        return DeterministicVerdict(decided=False, success=True)

    # --- layer 2: LLM --------------------------------------------------------
    async def _llm_verify(
        self, step: PlanStep, observation: dict[str, Any], *, tool_ok: bool
    ) -> VerificationVerdict:
        from app.agent.prompts import VERIFIER_SYSTEM, VERIFIER_USER, render

        observation_block = sanitize_untrusted(
            self._observation_text(observation)[:6000]
        ).as_prompt_block("tool_observation")
        prompt = render(
            VERIFIER_USER,
            goal=step.goal,
            expected_outcome=step.expected_outcome or "(not specified)",
            tool=step.tool,
            args=str(sanitize_untrusted(str(step.args)[:800]).text),
            ok=str(tool_ok).lower(),
            observation=observation_block,
        )
        try:
            return await self.llm.complete_structured(
                VerificationVerdict, prompt, system=VERIFIER_SYSTEM, max_repairs=1
            )
        except Exception as exc:  # noqa: BLE001 - verification must never crash a run
            logger.warning("LLM verification failed, falling back to tool status: %s", exc)
            return VerificationVerdict(
                success=tool_ok,
                confidence=0.4,
                reason="verifier unavailable; trusting the tool's own result",
            )

    # --- helpers --------------------------------------------------------------
    @staticmethod
    def _observation_text(observation: dict[str, Any]) -> str:
        parts: list[str] = []
        for key in ("summary", "text", "text_excerpt", "body_preview", "title", "url"):
            value = observation.get(key)
            if isinstance(value, str) and value:
                parts.append(value)
        items = observation.get("items")
        if isinstance(items, list):
            parts.append(str(items[:20]))
        if observation.get("structured") is not None:
            parts.append(str(observation["structured"])[:4000])
        return "\n".join(parts)

    @staticmethod
    def _looks_like_error_page(text: str) -> bool:
        lowered = text.lower()
        if any(fp in lowered for fp in ERROR_FALSE_POSITIVES):
            return False
        if any(m in lowered for m in STRONG_ERROR_MARKERS):
            return True
        # Weak markers alone are too noisy ("required" appears in normal forms),
        # so require two of them before we fail the step.
        weak = [m for m in WEAK_ERROR_MARKERS if m in lowered]
        return len(weak) >= 2

    @staticmethod
    def _money_close(expected: str, text: str) -> bool:
        """Accept 4.99 / $4.99 / 4.9900 as the same amount."""
        try:
            target = float(expected.replace("$", "").replace(",", ""))
        except ValueError:
            return True
        for match in NUMBER.findall(text.replace(",", "")):
            try:
                if abs(float(match) - target) < 0.005:
                    return True
            except ValueError:
                continue
        return False

    @staticmethod
    def _count_hint(observation: dict[str, Any], expected: int) -> bool | None:
        """True if a count is visible and matches, False if visible and wrong."""
        for key in ("count", "item_count"):
            value = observation.get(key)
            if isinstance(value, int):
                return value == expected
        items = observation.get("items")
        if isinstance(items, list) and items:
            return len(items) == expected
        return None

    @staticmethod
    def _expects_item(step: PlanStep) -> bool:
        goal = f"{step.goal} {step.expected_outcome}".lower()
        return any(
            verb in goal
            for verb in (
                "add to cart", "in the cart", "cart contains", "cart lists",
                "scrape", "collect", "gather",
            )
        )

    @staticmethod
    def _is_empty_result(observation: dict[str, Any]) -> bool:
        if observation.get("count") == 0:
            return True
        items = observation.get("items")
        if isinstance(items, list) and not items and observation.get("count") is not None:
            return True
        lowered = str(observation.get("text") or "").lower()
        return any(
            marker in lowered
            for marker in (
                "cart is empty", "your cart is empty", "no products matched",
                "0 line item", "no results",
            )
        )


__all__ = ["DeterministicVerdict", "Verifier"]
