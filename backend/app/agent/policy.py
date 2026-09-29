"""Deterministic risk classification — the engine behind the approval gate.

The rule that makes TaskPilot trustworthy is stated in one place, here:

    effective_risk = max(deterministic_rule_risk, planner_declared_risk)

and a step with ``effective_risk == high`` **always** requires a recorded human
approval before it can execute (``high`` is not configurable, by design).

The deterministic pass exists because LLMs are optimistic. A model that forgets to
mark "Place order" as dangerous must not be able to spend the user's money. The
rules are intentionally shallow, explainable and testable — they look at keywords,
selector shapes, HTTP verbs and filesystem effects, and each one that fires is
recorded with a human-readable reason so the approval card can *show its work*.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings, get_settings
from app.core.models import RiskLevel
from app.core.schemas import PlanStep
from app.tools.base import Tool

# ---------------------------------------------------------------------------
# rule vocabulary
# ---------------------------------------------------------------------------
# Payments / money movement. Firing here is high risk on its own.
PAYMENT_TERMS = (
    "buy", "purchase", "pay", "payment", "place order", "place the order",
    "submit order", "confirm order", "checkout", "check out", "pay now",
    "complete purchase", "complete order", "charge", "billing", "invoice",
    "transfer", "wire", "donate", "subscribe", "add payment",
)

# Irreversible content/state changes.
DESTRUCTIVE_TERMS = (
    "delete", "remove", "erase", "destroy", "drop", "purge", "unsubscribe",
    "cancel subscription", "close account", "deactivate", "revoke", "reset password",
    "overwrite", "truncate", "wipe", "terminate", "deregister", "withdraw",
)

# Communications that leave the user's control.
OUTBOUND_TERMS = (
    "send", "email", "e-mail", "sms", "message", "notify", "post", "publish",
    "tweet", "share", "comment", "reply", "invite", "submit", "apply",
    "register", "sign up", "signup", "enrol", "enroll", "confirm",
    "accept terms", "agree",
)

# Selectors that identify a control whose click is irreversible.
SUBMIT_SELECTOR = re.compile(
    r"(type\s*=\s*[\"']?submit|\[type=[\"']?submit|:submit|"
    r"#?(submit|place[-_]?order|pay|pay-?now|buy|checkout|confirm|delete|remove|"
    r"send|post|publish|signup|sign-?up|unsubscribe|register|submit[-_]?\w*))",
    re.IGNORECASE,
)

# A read-only verb in the goal makes an otherwise-scary word safe ("read the report").
BENIGN_CONTEXT = re.compile(
    r"\b(read|list|view|show|get|fetch|check|inspect|scrape|summar|monitor|"
    r"how many|count|report on)\b",
    re.IGNORECASE,
)

# Endpoints that smell like money even with a harmless-looking goal.
RISKY_PATH = re.compile(
    r"/(checkout|orders?|payments?|pay|purchase|billing|charges?|transfers?|"
    r"refunds?|subscriptions?)(/|$|\?)",
    re.IGNORECASE,
)

DELETING_METHODS = {"DELETE"}
MUTATING_METHODS = {"POST", "PUT", "PATCH"}
FILE_OVERWRITE_TERMS = ("overwrite", "replace", "clobber")


@dataclass(slots=True)
class RiskReason:
    rule: str
    detail: str
    risk: RiskLevel

    def to_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "detail": self.detail, "risk": self.risk.value}


@dataclass(slots=True)
class PolicyDecision:
    """The verdict for one step."""

    declared_risk: RiskLevel = RiskLevel.LOW
    rule_risk: RiskLevel = RiskLevel.LOW
    effective_risk: RiskLevel = RiskLevel.LOW
    needs_approval: bool = False
    reasons: list[RiskReason] = field(default_factory=list)
    llm_risk: RiskLevel | None = None
    blocked: bool = False
    block_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "declared_risk": self.declared_risk.value,
            "rule_risk": self.rule_risk.value,
            "llm_risk": self.llm_risk.value if self.llm_risk else None,
            "effective_risk": self.effective_risk.value,
            "needs_approval": self.needs_approval,
            "reasons": [r.to_dict() for r in self.reasons],
            "blocked": self.blocked,
            "block_reason": self.block_reason,
        }

    def explanation(self) -> str:
        """One-line, human sentence for logs and the approval card."""
        if not self.reasons:
            return f"{self.effective_risk.value} risk — no risky patterns matched"
        top = [r.detail for r in self.reasons if r.risk.rank >= self.effective_risk.rank]
        return "; ".join(top or [r.detail for r in self.reasons])


class PolicyEngine:
    """Pure, deterministic, synchronous. No I/O, no LLM, trivially testable."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # --- public ---------------------------------------------------------------
    def classify(
        self,
        step: PlanStep | dict[str, Any],
        tool: Tool | None,
        *,
        settings: Settings | None = None,
        existing_paths: set[str] | None = None,
        llm_risk: RiskLevel | str | None = None,
    ) -> PolicyDecision:
        settings = settings or self.settings
        raw = step if isinstance(step, dict) else None
        goal = str(getattr(step, "goal", "") or (raw or {}).get("goal", "") or "")
        args: dict[str, Any] = getattr(step, "args", None) or (raw or {}).get("args", {}) or {}
        declared_raw = getattr(step, "declared_risk", None) or (raw or {}).get("risk", "low")
        declared = RiskLevel(declared_raw if declared_raw in {"low", "medium", "high"} else "low")
        tool_name = tool.name if tool else str(getattr(step, "tool", "") or (raw or {}).get("tool", ""))
        tool_risk = tool.risk_level if tool else RiskLevel.LOW

        reasons: list[RiskReason] = []
        rule_risk = RiskLevel.LOW

        def raise_to(level: RiskLevel, rule: str, detail: str) -> None:
            nonlocal rule_risk
            if level.rank > rule_risk.rank:
                rule_risk = level
            reasons.append(RiskReason(rule=rule, detail=detail, risk=level))

        haystack = self._haystack(goal, args)
        benign = bool(BENIGN_CONTEXT.search(goal))

        # 1. tool-declared baseline ------------------------------------------
        if tool_risk.rank > RiskLevel.LOW.rank:
            raise_to(
                tool_risk,
                "tool_baseline",
                f"tool {tool_name!r} is declared {tool_risk.value} risk",
            )
        if tool is not None and tool.requires_approval_hint:
            raise_to(
                RiskLevel.MEDIUM,
                "tool_hint",
                f"tool {tool_name!r} can perform irreversible actions",
            )

        # 2. money -----------------------------------------------------------
        money_hits = self._terms(haystack, PAYMENT_TERMS)
        if money_hits:
            raise_to(
                RiskLevel.HIGH,
                "payment_terms",
                f"step involves payment/purchase language: {_quote(money_hits)}",
            )

        # 3. destructive -----------------------------------------------------
        destructive_hits = self._terms(haystack, DESTRUCTIVE_TERMS)
        if destructive_hits:
            raise_to(
                RiskLevel.HIGH,
                "destructive_terms",
                f"step can permanently remove data: {_quote(destructive_hits)}",
            )

        # 4. outbound / submission -------------------------------------------
        outbound_hits = self._terms(haystack, OUTBOUND_TERMS)
        if outbound_hits:
            level = RiskLevel.MEDIUM if benign and not money_hits else RiskLevel.HIGH
            raise_to(
                level,
                "outbound_terms",
                f"step sends information or submits something: {_quote(outbound_hits)}",
            )

        # 5. selector shape --------------------------------------------------
        selectors = self._selectors(args)
        submit_selectors = [s for s in selectors if SUBMIT_SELECTOR.search(s)]
        if submit_selectors:
            raise_to(
                RiskLevel.HIGH,
                "submit_control",
                f"target looks like a submit/pay/delete control: {_quote(submit_selectors)}",
            )

        return self._finish(
            step,
            raw,
            settings,
            declared,
            rule_risk,
            reasons,
            llm_risk,
            existing_paths or set(),
            tool_name,
        )

    def _finish(
        self,
        step: PlanStep | dict[str, Any],
        raw: dict[str, Any] | None,
        settings: Settings,
        declared: RiskLevel,
        rule_risk: RiskLevel,
        reasons: list[RiskReason],
        llm_risk: RiskLevel | str | None,
        existing_paths: set[str],
        tool_name: str,
    ) -> PolicyDecision:
        """Steps 6–10: shape flags, filesystem effects, LLM opinion, merge."""
        args: dict[str, Any] = getattr(step, "args", None) or (raw or {}).get("args", {}) or {}
        goal = str(getattr(step, "goal", "") or (raw or {}).get("goal", "") or "")
        haystack = self._haystack(goal, args)

        def raise_to(level: RiskLevel, rule: str, detail: str) -> None:
            nonlocal rule_risk
            if level.rank > rule_risk.rank:
                rule_risk = level
            reasons.append(RiskReason(rule=rule, detail=detail, risk=level))

        # 6. explicit submit flags / HTTP verbs ------------------------------
        if args.get("submit") is True:
            raise_to(RiskLevel.HIGH, "submit_flag", "step explicitly submits a form (submit=true)")
        method = str(args.get("method", "")).upper()
        if method in MUTATING_METHODS:
            raise_to(RiskLevel.MEDIUM, "http_mutating_method", f"HTTP {method} changes server state")
        if method in DELETING_METHODS:
            raise_to(RiskLevel.HIGH, "http_delete", "HTTP DELETE is irreversible")
        url = str(args.get("url") or args.get("action") or "")
        if url and method not in {"", "GET", "HEAD"} and RISKY_PATH.search(url):
            raise_to(
                RiskLevel.HIGH,
                "risky_endpoint",
                f"non-GET request to a purchase/order endpoint ({url})",
            )

        # 7. filesystem effects ----------------------------------------------
        path = str(args.get("path") or args.get("filename") or "")
        if path and tool_name in {"file_write", "write_report"} and not args.get("append"):
            if path in existing_paths:
                raise_to(
                    RiskLevel.HIGH, "file_overwrite", f"would overwrite the existing file {path!r}"
                )
            elif any(term in haystack for term in FILE_OVERWRITE_TERMS):
                raise_to(
                    RiskLevel.HIGH, "file_overwrite_intent", f"step intends to overwrite {path!r}"
                )

        # 8. explicit approval request from the planner -----------------------
        if bool(getattr(step, "needs_approval", False) or (raw or {}).get("needs_approval")):
            raise_to(
                RiskLevel.HIGH,
                "planner_requested_approval",
                "the planner flagged this step for human approval",
            )

        # 9. LLM second opinion ----------------------------------------------
        llm_level: RiskLevel | None = None
        if llm_risk is not None:
            try:
                llm_level = llm_risk if isinstance(llm_risk, RiskLevel) else RiskLevel(str(llm_risk))
                if llm_level.rank > RiskLevel.LOW.rank:
                    raise_to(
                        llm_level,
                        "llm_assessment",
                        f"risk model classified this as {llm_level.value} risk",
                    )
            except ValueError:  # pragma: no cover - defensive
                llm_level = None

        # 10. merge — rules and the planner can only ever RAISE the risk, never lower it
        effective = RiskLevel.max_of(rule_risk, declared, llm_level)
        return PolicyDecision(
            declared_risk=declared,
            rule_risk=rule_risk,
            effective_risk=effective,
            needs_approval=self.approval_required(effective, settings),
            reasons=reasons,
            llm_risk=llm_level,
        )

    # --- helpers --------------------------------------------------------------
    @staticmethod
    def approval_required(risk: RiskLevel, settings: Settings) -> bool:
        """The rule that is never negotiable: **high risk always pauses**.

        Medium risk is the "reversible but writes to a live system" band, so it
        follows the strict-mode toggle. Low risk never pauses.
        """
        if risk is RiskLevel.HIGH:
            return True
        if risk is RiskLevel.MEDIUM:
            return not settings.auto_approve_low_risk
        return False

    @staticmethod
    def _haystack(goal: str, args: dict[str, Any]) -> str:
        try:
            rendered = json.dumps(args, default=str)
        except (TypeError, ValueError):  # pragma: no cover
            rendered = str(args)
        return f"{goal} {rendered}".lower()

    @staticmethod
    def _terms(haystack: str, terms: tuple[str, ...]) -> list[str]:
        return [t for t in terms if re.search(rf"\b{re.escape(t)}\b", haystack)]

    @staticmethod
    def _selectors(args: dict[str, Any]) -> list[str]:
        found: list[str] = []
        for key in ("selector", "submit_selector", "target", "css", "element"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                found.append(value.strip())
        for f in args.get("fields") or []:
            if isinstance(f, dict) and isinstance(f.get("selector"), str):
                found.append(f["selector"])
        if isinstance(args.get("text"), str):
            found.append(args["text"])
        return found


def check_plan(
    steps: list[PlanStep],
    *,
    tools: dict[str, Tool],
    settings: Settings | None = None,
) -> list[PolicyDecision]:
    """Classify a whole plan in order. Convenience for the planner and the UI."""
    engine = PolicyEngine(settings)
    return [engine.classify(step, tools.get(step.tool), settings=settings) for step in steps]


def _quote(items: list[str], limit: int = 3) -> str:
    shown = ", ".join(f"“{i}”" for i in items[:limit])
    extra = f" +{len(items) - limit} more" if len(items) > limit else ""
    return f"{shown}{extra}"


_engine: PolicyEngine | None = None


def get_policy_engine() -> PolicyEngine:
    global _engine
    if _engine is None:
        _engine = PolicyEngine()
    return _engine


def reset_policy_engine() -> None:
    """Test helper."""
    global _engine
    _engine = None


__all__ = [
    "BENIGN_CONTEXT",
    "DESTRUCTIVE_TERMS",
    "OUTBOUND_TERMS",
    "PAYMENT_TERMS",
    "PolicyDecision",
    "PolicyEngine",
    "RiskReason",
    "SUBMIT_SELECTOR",
    "check_plan",
    "get_policy_engine",
    "reset_policy_engine",
]
