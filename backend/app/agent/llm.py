"""Provider-agnostic LLM client with structured-output repair.

Three things make this module worth its length:

1. **One interface, four backends.** Anthropic / OpenAI-compatible / Gemini are
   all plain ``httpx`` calls, so there is no SDK version drift and the whole
   thing is mockable in tests with a single transport stub. The fourth backend,
   :class:`ScriptedLLM`, is the deterministic demo/eval brain.
2. **Structured output with repair.** ``complete_structured`` asks for JSON,
   validates it with Pydantic, and on failure re-prompts *with the validation
   error* (bounded retries). Models fix their own JSON when shown the error.
3. **Never leak keys.** Provider errors are re-raised as :class:`LLMError` with
   scrubbed messages, and the API key only ever appears in a header.
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.core.config import Settings, get_settings
from app.core.errors import LLMError, LLMOutputError
from app.core.logging import get_logger, log_event, scrub
from app.core.security import sanitize_untrusted

logger = get_logger(__name__)


T = TypeVar("T", bound=BaseModel)

DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-5-20250929",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.5-flash",
    "scripted": "taskpilot-scripted-v1",
}

MODEL_CHOICES: dict[str, list[str]] = {
    "anthropic": [
        "claude-sonnet-4-5-20250929",
        "claude-opus-4-1-20250805",
        "claude-haiku-4-5-20251001",
    ],
    "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4.1-mini", "o4-mini"],
    "gemini": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"],
    "scripted": ["taskpilot-scripted-v1"],
}

PROVIDER_LABELS = {
    "anthropic": "Anthropic Claude",
    "openai": "OpenAI / compatible",
    "gemini": "Google Gemini",
    "scripted": "Deterministic demo brain",
}


@dataclass(slots=True)
class LLMMessage:
    role: str  # system | user | assistant
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(slots=True)
class LLMResponse:
    text: str
    provider: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    stop_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------
_FENCE = re.compile(r"```(?:json|json5)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> Any:
    """Best-effort JSON extraction from model prose.

    Handles fenced blocks, leading chatter, trailing commentary and the two
    classic LLM sins: single-quoted keys and trailing commas.
    """
    if not text or not text.strip():
        raise ValueError("empty model output")
    candidates: list[str] = []
    stripped = text.strip()
    candidates.append(stripped)
    candidates.extend(m.group(1).strip() for m in _FENCE.finditer(stripped))

    for opener, closer in (("{", "}"), ("[", "]")):
        start = stripped.find(opener)
        if start == -1:
            continue
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(stripped)):
            ch = stripped[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    candidates.append(stripped[start : i + 1])
                    break

    last_error: Exception | None = None
    for candidate in candidates:
        if not candidate:
            continue
        for variant in (candidate, _repair_json_text(candidate)):
            try:
                return json.loads(variant)
            except (json.JSONDecodeError, ValueError) as exc:
                last_error = exc
                continue
    raise ValueError(f"could not parse JSON from model output: {last_error}")


def _repair_json_text(text: str) -> str:
    """Fix the mechanical mistakes models make; never invents content."""
    repaired = text.strip()
    repaired = re.sub(r"^[^{[]*", "", repaired, count=1)
    repaired = re.sub(r"[^}\]]*$", "", repaired, count=1)
    repaired = re.sub(r",(\s*[}\]])", r"\1", repaired)          # trailing commas
    repaired = re.sub(r"//[^\n]*", "", repaired)                 # line comments
    repaired = re.sub(r"/\*.*?\*/", "", repaired, flags=re.DOTALL)
    repaired = repaired.replace("True", "true").replace("False", "false").replace("None", "null")
    return repaired


class LLMClient(abc.ABC):
    """Interface every provider implements."""

    provider: str = "unknown"
    model: str = "unknown"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.stats = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "errors": 0}

    # --- provider hook --------------------------------------------------------
    @abc.abstractmethod
    async def _complete(
        self,
        messages: list[LLMMessage],
        *,
        system: str | None,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> LLMResponse:
        """Single provider round-trip."""

    # --- public ---------------------------------------------------------------
    async def complete(
        self,
        messages: list[LLMMessage] | str,
        *,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        if isinstance(messages, str):
            messages = [LLMMessage(role="user", content=messages)]
        temperature = self.settings.llm_temperature if temperature is None else temperature
        max_tokens = self.settings.llm_max_tokens if max_tokens is None else max_tokens

        last_error: Exception | None = None
        attempts = max(1, self.settings.llm_max_retries)
        for attempt in range(attempts):
            try:
                started = time.perf_counter()
                response = await self._complete(
                    messages,
                    system=system,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    json_mode=json_mode,
                )
                response.latency_ms = round((time.perf_counter() - started) * 1000, 1)
                self.stats["calls"] += 1
                self.stats["tokens_in"] += response.tokens_in
                self.stats["tokens_out"] += response.tokens_out
                log_event(
                    logger,
                    logging.INFO,
                    "llm call",
                    provider=self.provider,
                    model=response.model,
                    tokens_in=response.tokens_in,
                    tokens_out=response.tokens_out,
                    duration_ms=response.latency_ms,
                    attempt=attempt + 1,
                )
                return response
            except LLMError:
                raise
            except (httpx.HTTPError, TimeoutError, OSError) as exc:
                last_error = exc
                delay = min(2**attempt, 8)
                logger.warning(
                    "llm transport error (attempt %d/%d): %s", attempt + 1, attempts, exc
                )
                if attempt + 1 < attempts:
                    await asyncio.sleep(delay)
        self.stats["errors"] += 1
        raise LLMError(
            f"{self.provider} request failed after {attempts} attempts: {last_error}"
        )

    async def complete_structured(
        self,
        schema: type[T],
        messages: list[LLMMessage] | str,
        *,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        max_repairs: int = 2,
    ) -> T:
        """Get a validated instance of ``schema``, repairing bad JSON if needed."""
        if isinstance(messages, str):
            messages = [LLMMessage(role="user", content=messages)]
        spec = json.dumps(schema.model_json_schema())
        instruction = (
            "Respond with a single JSON object and nothing else. It MUST validate against "
            f"this JSON Schema:\n{spec}"
        )
        convo = list(messages)
        system = f"{system}\n\n{instruction}" if system else instruction

        for attempt in range(max_repairs + 1):
            response = await self.complete(
                convo,
                system=system,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=True,
            )
            try:
                payload = extract_json(response.text)
                if not isinstance(payload, dict):
                    raise ValueError(f"expected a JSON object, got {type(payload).__name__}")
                return schema.model_validate(payload)
            except (ValueError, ValidationError) as exc:
                logger.warning(
                    "structured output invalid (attempt %d/%d): %s",
                    attempt + 1,
                    max_repairs + 1,
                    exc,
                )
                if attempt >= max_repairs:
                    self.stats["errors"] += 1
                    raise LLMOutputError(
                        f"{self.provider} returned output that failed {schema.__name__} "
                        f"validation after {max_repairs + 1} attempts: {exc}",
                        details={
                            "schema": schema.__name__,
                            "raw_preview": response.text[:800],
                        },
                    ) from exc
                convo = [
                    *convo,
                    LLMMessage(role="assistant", content=response.text[:4000]),
                    LLMMessage(
                        role="user",
                        content=(
                            "That response was rejected by schema validation:\n"
                            f"{exc}\n\nReturn ONLY corrected JSON that validates against the "
                            f"{schema.__name__} schema. No prose, no markdown fences."
                        ),
                    ),
                ]
        raise LLMOutputError("unreachable")  # pragma: no cover

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "label": PROVIDER_LABELS.get(self.provider, self.provider),
            "model": self.model,
            "default_model": DEFAULT_MODELS.get(self.provider, ""),
            "models": MODEL_CHOICES.get(self.provider, []),
            "configured": True,
            "stats": dict(self.stats),
        }


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------
class AnthropicClient(LLMClient):
    provider = "anthropic"

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings)
        self.model = self.settings.llm_model or DEFAULT_MODELS["anthropic"]
        self.api_key = self.settings.anthropic_api_key
        self.base_url = (self.settings.llm_base_url or "https://api.anthropic.com").rstrip("/")
        self.api_version = "2023-06-01"

    async def _complete(
        self,
        messages: list[LLMMessage],
        *,
        system: str | None,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> LLMResponse:
        if not self.api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [m.to_dict() for m in messages if m.role != "system"],
        }
        if system:
            body["system"] = system
        if json_mode:
            # Prefill nudges Claude into emitting bare JSON immediately.
            body["messages"] = [*body["messages"], {"role": "assistant", "content": "{"}]

        async with httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url}/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": self.api_version,
                    "content-type": "application/json",
                },
                json=body,
            )
        if response.status_code >= 400:
            raise LLMError(f"anthropic HTTP {response.status_code}: {_safe_error(response)}")

        payload = response.json()
        blocks = payload.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        if json_mode and text and not text.lstrip().startswith("{"):
            text = "{" + text
        usage = payload.get("usage") or {}
        return LLMResponse(
            text=text,
            provider=self.provider,
            model=payload.get("model", self.model),
            tokens_in=int(usage.get("input_tokens") or 0),
            tokens_out=int(usage.get("output_tokens") or 0),
            stop_reason=payload.get("stop_reason"),
        )


# ---------------------------------------------------------------------------
# OpenAI (and any OpenAI-compatible gateway)
# ---------------------------------------------------------------------------
class OpenAIClient(LLMClient):
    provider = "openai"

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings)
        self.model = self.settings.llm_model or DEFAULT_MODELS["openai"]
        self.api_key = self.settings.openai_api_key
        self.base_url = (self.settings.llm_base_url or "https://api.openai.com").rstrip("/")

    async def _complete(
        self,
        messages: list[LLMMessage],
        *,
        system: str | None,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> LLMResponse:
        if not self.api_key:
            raise LLMError("OPENAI_API_KEY is not set")
        payload_messages: list[dict[str, str]] = []
        if system:
            payload_messages.append({"role": "system", "content": system})
        payload_messages.extend(m.to_dict() for m in messages)

        body: dict[str, Any] = {
            "model": self.model,
            "messages": payload_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        async with httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
        if response.status_code >= 400:
            raise LLMError(f"openai HTTP {response.status_code}: {_safe_error(response)}")
        data = response.json()
        choice = (data.get("choices") or [{}])[0]
        usage = data.get("usage") or {}
        return LLMResponse(
            text=(choice.get("message") or {}).get("content") or "",
            provider=self.provider,
            model=data.get("model", self.model),
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
            stop_reason=choice.get("finish_reason"),
        )


# ---------------------------------------------------------------------------
# Google Gemini
# ---------------------------------------------------------------------------
class GeminiClient(LLMClient):
    provider = "gemini"

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings)
        self.model = self.settings.llm_model or DEFAULT_MODELS["gemini"]
        self.api_key = self.settings.gemini_api_key
        self.base_url = (
            self.settings.llm_base_url or "https://generativelanguage.googleapis.com"
        ).rstrip("/")

    async def _complete(
        self,
        messages: list[LLMMessage],
        *,
        system: str | None,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> LLMResponse:
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY is not set")
        contents = [
            {
                "role": "model" if m.role == "assistant" else "user",
                "parts": [{"text": m.content}],
            }
            for m in messages
            if m.role != "system"
        ]
        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}

        async with httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url}/v1beta/models/{self.model}:generateContent",
                headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
                json=body,
            )
        if response.status_code >= 400:
            raise LLMError(f"gemini HTTP {response.status_code}: {_safe_error(response)}")
        data = response.json()
        candidates = data.get("candidates") or [{}]
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        usage = data.get("usageMetadata") or {}
        return LLMResponse(
            text="".join(p.get("text", "") for p in parts if isinstance(p, dict)),
            provider=self.provider,
            model=self.model,
            tokens_in=int(usage.get("promptTokenCount") or 0),
            tokens_out=int(usage.get("candidatesTokenCount") or 0),
            stop_reason=candidates[0].get("finishReason"),
        )


def _safe_error(response: httpx.Response) -> str:
    """Provider error bodies can echo keys back; scrub before surfacing."""
    try:
        body = response.text[:500]
    except Exception:  # pragma: no cover
        body = "<unreadable>"
    return str(scrub(body))


# __APPEND__


def _scripted_client(settings: Settings) -> "ScriptedLLM":
    from app.agent.scripted import ScriptedLLM

    return ScriptedLLM(settings)


# Provider name -> builder. Extension point for an OpenAI-compatible gateway.
_BUILDERS: dict[str, Any] = {
    "anthropic": AnthropicClient,
    "openai": OpenAIClient,
    "gemini": GeminiClient,
    "scripted": _scripted_client,
}

_cached: dict[tuple[str, str], LLMClient] = {}


def build_llm(
    provider: str | None = None,
    model: str | None = None,
    settings: Settings | None = None,
) -> LLMClient:
    """Instantiate a client, transparently falling back to scripted mode.

    The fallback is what keeps the public demo alive when API credits run out or
    no key is configured. It is logged loudly and surfaced in ``/health`` so it
    can never be mistaken for a real model.
    """
    settings = settings or get_settings()
    provider = (provider or settings.llm_provider or "anthropic").lower()
    key = settings.provider_key(provider)

    if provider != "scripted" and not key and settings.allow_scripted_fallback:
        log_event(
            logger,
            logging.WARNING,
            f"no API key for {provider}; falling back to the deterministic scripted brain",
            provider=provider,
        )
        provider = "scripted"
        key = "scripted"

    builder = _BUILDERS.get(provider)
    if builder is None:
        raise LLMError(f"Unknown LLM provider {provider!r}. Known: {sorted(_BUILDERS)}")

    client = builder(settings)
    if model and getattr(client, "model", "") != model:
        # Model selection is a per-client attribute; copy rather than mutate shared state.
        import copy

        client = copy.copy(client)
        client.model = model
    return client


def get_llm(
    provider: str | None = None,
    model: str | None = None,
    settings: Settings | None = None,
) -> LLMClient:
    """Cached accessor — one HTTP-configured client per (provider, model)."""
    settings = settings or get_settings()
    resolved = (provider or settings.llm_provider or "anthropic").lower()
    if resolved != "scripted" and not settings.provider_key(resolved) and settings.allow_scripted_fallback:
        resolved = "scripted"
    resolved_model = model or settings.llm_model
    cache_key = (resolved, resolved_model or "")
    if cache_key not in _cached:
        _cached[cache_key] = build_llm(resolved, resolved_model, settings)
    return _cached[cache_key]


def reset_llm() -> None:
    """Test helper: drop cached clients."""
    _cached.clear()


def provider_status(settings: Settings | None = None) -> list[dict[str, Any]]:
    """Per-provider configuration state for the Settings page."""
    settings = settings or get_settings()
    active = (settings.llm_provider or "").lower()
    if active != "scripted" and not settings.provider_key(active) and settings.allow_scripted_fallback:
        active = "scripted"
    out: list[dict[str, Any]] = []
    for provider, default_model in DEFAULT_MODELS.items():
        configured = provider == "scripted" or bool(settings.provider_key(provider))
        out.append(
            {
                "id": provider,
                "label": PROVIDER_LABELS[provider],
                "default_model": default_model,
                "configured": configured,
                "models": MODEL_CHOICES[provider],
                "active": provider == active,
            }
        )
    return out
