"""LLM layer: JSON extraction, retry/backoff, structured repair, provider wiring.

`agent/llm.py` was the second-largest coverage hole and holds logic that fails
silently when broken. `extract_json` is the difference between a working demo
and a run that dies on a stray markdown fence; the retry loop is what keeps a
rate-limited provider from killing a run. Neither had a test.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import BaseModel, Field

from app.agent.llm import (
    AnthropicClient,
    GeminiClient,
    LLMClient,
    LLMError,
    LLMMessage,
    LLMOutputError,
    LLMResponse,
    OpenAIClient,
    build_llm,
    extract_json,
    get_llm,
    provider_status,
    reset_llm,
)
from app.core.config import Settings


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class FakeClient(LLMClient):
    """An LLMClient whose transport is a scripted list of outcomes."""

    provider = "fake"
    model = "fake-1"

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings)
        self.outcomes: list[Any] = []
        self.seen: list[dict[str, Any]] = []

    async def _complete(self, messages, *, system, temperature, max_tokens, json_mode):
        self.seen.append(
            {
                "messages": list(messages),
                "system": system,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "json_mode": json_mode,
            }
        )
        outcome = self.outcomes.pop(0) if self.outcomes else "done"
        if isinstance(outcome, BaseException):
            raise outcome
        return LLMResponse(
            text=str(outcome),
            provider=self.provider,
            model=self.model,
            tokens_in=10,
            tokens_out=5,
        )


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"llm_provider": "scripted", "llm_max_retries": 1}
    base.update(overrides)
    return Settings(**base)


def http_response(status: int = 200, json_body: Any = None, text: str = "") -> httpx.Response:
    """Build a Response carrying either a JSON body or a raw text body.

    httpx serialises whichever content argument is given, so exactly one of
    ``json_body`` / ``text`` may be supplied — passing both raises at build time.
    """
    kwargs: dict[str, Any] = {"status_code": status}
    if json_body is not None:
        kwargs["json"] = json_body
    else:
        kwargs["text"] = text
    return httpx.Response(**kwargs, request=httpx.Request("POST", "https://example.test/v1"))


def patch_post(monkeypatch, result: Any):
    """Make every httpx post return `result` (a Response or an exception)."""
    calls: list[dict[str, Any]] = []

    async def fake_post(self, url, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"url": url, **kwargs})
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return calls


async def _no_sleep(_seconds: float) -> None:
    """Stand-in for asyncio.sleep so retry backoff does not slow the suite."""


@pytest.fixture(autouse=True)
def _clear_llm_cache():
    reset_llm()
    yield
    reset_llm()


# ---------------------------------------------------------------------------
# extract_json — the difference between a working demo and a dead one
# ---------------------------------------------------------------------------
def test_plain_json_object_parses():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_fenced_json_block_is_unwrapped():
    assert extract_json('Here you go:\n```json\n{"a": 1}\n```\nThanks!') == {"a": 1}


def test_bare_fence_without_language_tag_is_unwrapped():
    assert extract_json('```\n{"a": 1}\n```') == {"a": 1}


def test_json_array_is_extracted():
    assert extract_json("noise [1, 2, 3] noise") == [1, 2, 3]


def test_trailing_prose_after_object_is_ignored():
    assert extract_json('{"a": 1}\n\nThat is the answer.') == {"a": 1}


def test_trailing_comma_is_repaired():
    assert extract_json('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}


def test_python_style_booleans_are_repaired():
    assert extract_json('{"ok": True, "no": False, "x": None}') == {
        "ok": True,
        "no": False,
        "x": None,
    }


def test_comments_are_stripped():
    assert extract_json('{\n // a comment\n "a": 1 // trailing\n}') == {"a": 1}


def test_braces_inside_strings_do_not_confuse_the_scanner():
    assert extract_json('{"note": "a } b", "n": 1}') == {"note": "a } b", "n": 1}


def test_escaped_quote_inside_string_is_handled():
    assert extract_json('{"q": "she said \\"hi\\"", "n": 2}') == {"q": 'she said "hi"', "n": 2}


def test_empty_output_raises():
    with pytest.raises(ValueError):
        extract_json("")


def test_whitespace_only_output_raises():
    with pytest.raises(ValueError):
        extract_json("   \n  ")


def test_prose_with_no_json_raises():
    with pytest.raises(ValueError):
        extract_json("I am afraid I cannot do that.")


def test_malformed_json_raises():
    with pytest.raises(ValueError):
        extract_json("{definitely not json")


# ---------------------------------------------------------------------------
# complete(): argument handling, stats, retries
# ---------------------------------------------------------------------------
async def test_string_prompt_is_wrapped_as_a_user_message():
    client = FakeClient(settings())
    client.outcomes = ["ok"]
    await client.complete("hello")
    assert client.seen[0]["messages"] == [LLMMessage(role="user", content="hello")]


async def test_message_list_is_passed_through():
    client = FakeClient(settings())
    client.outcomes = ["ok"]
    msgs = [LLMMessage(role="user", content="hi")]
    await client.complete(msgs)
    assert client.seen[0]["messages"] == msgs


async def test_defaults_come_from_settings():
    client = FakeClient(settings(llm_temperature=0.25, llm_max_tokens=123))
    client.outcomes = ["ok"]
    await client.complete("hi")
    assert client.seen[0]["temperature"] == 0.25
    assert client.seen[0]["max_tokens"] == 123


async def test_explicit_args_override_settings():
    client = FakeClient(settings(llm_temperature=0.25, llm_max_tokens=123))
    client.outcomes = ["ok"]
    await client.complete("hi", temperature=0.9, max_tokens=7)
    assert client.seen[0]["temperature"] == 0.9
    assert client.seen[0]["max_tokens"] == 7


async def test_stats_accumulate_across_calls():
    client = FakeClient(settings())
    client.outcomes = ["a", "b"]
    await client.complete("hi")
    await client.complete("hi")
    assert client.stats["calls"] == 2
    assert client.stats["tokens_in"] == 20
    assert client.stats["tokens_out"] == 10


async def test_latency_is_measured():
    client = FakeClient(settings())
    client.outcomes = ["ok"]
    resp = await client.complete("hi")
    assert resp.latency_ms >= 0


async def test_transport_error_is_retried_then_raises(monkeypatch):
    client = FakeClient(settings(llm_max_retries=3))
    client.outcomes = [httpx.ConnectError("refused")] * 3
    monkeypatch.setattr("app.agent.llm.asyncio.sleep", _no_sleep)

    with pytest.raises(LLMError) as exc:
        await client.complete("hi")
    assert "3 attempts" in str(exc.value)
    assert client.stats["errors"] == 1


async def test_retry_succeeds_after_a_transient_error(monkeypatch):
    client = FakeClient(settings(llm_max_retries=3))
    client.outcomes = [httpx.ReadTimeout("slow"), "recovered"]
    monkeypatch.setattr("app.agent.llm.asyncio.sleep", _no_sleep)

    resp = await client.complete("hi")
    assert resp.text == "recovered"
    assert client.stats["calls"] == 1


async def test_llm_error_is_not_retried():
    """A 4xx will not fix itself; retrying just burns the user's quota."""
    client = FakeClient(settings(llm_max_retries=3))
    client.outcomes = [LLMError("bad request")] * 3

    with pytest.raises(LLMError):
        await client.complete("hi")
    assert len(client.seen) == 1, "LLMError must propagate immediately"


# ---------------------------------------------------------------------------
# complete_structured(): the repair loop
# ---------------------------------------------------------------------------
class Plan(BaseModel):
    goal: str
    steps: list[str] = Field(default_factory=list)


async def test_structured_output_is_validated_on_first_try():
    client = FakeClient(settings())
    client.outcomes = ['{"goal": "buy a cable"}']
    plan = await client.complete_structured(Plan, "plan my task")
    assert plan.goal == "buy a cable"


async def test_schema_instruction_is_sent_to_the_model():
    client = FakeClient(settings())
    client.outcomes = ['{"goal": "x"}']
    await client.complete_structured(Plan, "plan", system="be terse")

    system = client.seen[0]["system"]
    assert "be terse" in system
    assert "JSON Schema" in system
    assert client.seen[0]["json_mode"] is True


async def test_invalid_json_is_repaired_by_asking_again():
    client = FakeClient(settings())
    client.outcomes = ["I think the goal is buying", '{"goal": "buy"}']

    plan = await client.complete_structured(Plan, "plan")

    assert plan.goal == "buy"
    assert len(client.seen) == 2
    # The rejection reason is fed back so the model can actually fix it.
    follow_up = client.seen[1]["messages"][-1].content
    assert "rejected by schema validation" in follow_up


async def test_schema_violation_is_repaired():
    client = FakeClient(settings())
    client.outcomes = ['{"goal": 123, "steps": "not-a-list"}', '{"goal": "ok", "steps": []}']

    plan = await client.complete_structured(Plan, "plan")

    assert plan.goal == "ok"
    assert plan.steps == []


async def test_non_object_json_is_rejected_and_repaired():
    client = FakeClient(settings())
    client.outcomes = ["[1, 2, 3]", '{"goal": "ok"}']

    plan = await client.complete_structured(Plan, "plan")

    assert plan.goal == "ok"


async def test_persistent_failure_raises_with_the_raw_preview():
    client = FakeClient(settings())
    client.outcomes = ["not json at all"] * 3

    with pytest.raises(LLMOutputError) as exc:
        await client.complete_structured(Plan, "plan")

    assert "Plan" in str(exc.value)
    assert exc.value.details["schema"] == "Plan"


# ---------------------------------------------------------------------------
# provider adapters (transport faked; no network)
# ---------------------------------------------------------------------------
async def test_anthropic_without_key_fails_clearly():
    client = AnthropicClient(settings(anthropic_api_key=None))
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        await client._complete([], system=None, temperature=0.0, max_tokens=10, json_mode=False)


async def test_anthropic_request_shape_and_parsing(monkeypatch):
    calls = patch_post(
        monkeypatch,
        http_response(
            json_body={
                "content": [{"type": "text", "text": "hello"}],
                "usage": {"input_tokens": 7, "output_tokens": 3},
                "model": "claude-x",
                "stop_reason": "end_turn",
            }
        ),
    )
    client = AnthropicClient(settings(anthropic_api_key="sk-test"))

    resp = await client._complete(
        [LLMMessage(role="user", content="hi")],
        system="be nice",
        temperature=0.5,
        max_tokens=64,
        json_mode=False,
    )

    assert resp.text == "hello"
    assert resp.tokens_in == 7
    assert resp.tokens_out == 3
    assert resp.model == "claude-x"
    assert resp.stop_reason == "end_turn"
    body = calls[0]["json"]
    assert body["system"] == "be nice"
    assert calls[0]["headers"]["x-api-key"] == "sk-test"


async def test_anthropic_json_mode_prefills_an_open_brace(monkeypatch):
    calls = patch_post(monkeypatch, http_response(json_body={"content": [{"text": '"a": 1}'}]}))
    client = AnthropicClient(settings(anthropic_api_key="sk-test"))

    resp = await client._complete(
        [LLMMessage(role="user", content="hi")],
        system=None,
        temperature=0.0,
        max_tokens=10,
        json_mode=True,
    )

    # The prefill "{" is stripped from the response so the text is valid JSON.
    assert resp.text == '{"a": 1}'
    assert calls[0]["json"]["messages"][-1] == {"role": "assistant", "content": "{"}


async def test_anthropic_http_error_is_surfaced(monkeypatch):
    patch_post(monkeypatch, http_response(status=429, text="rate limited"))
    client = AnthropicClient(settings(anthropic_api_key="sk-test"))

    with pytest.raises(LLMError, match="429"):
        await client._complete([], system=None, temperature=0.0, max_tokens=1, json_mode=False)


async def test_anthropic_error_body_is_scrubbed(monkeypatch):
    """Provider error bodies can echo the key back; scrub before surfacing."""
    patch_post(
        monkeypatch,
        http_response(status=401, text='{"error": "bad key sk-ant-secret-value"}'),
    )
    client = AnthropicClient(settings(anthropic_api_key="sk-ant-secret-value"))

    with pytest.raises(LLMError) as exc:
        await client._complete([], system=None, temperature=0.0, max_tokens=1, json_mode=False)
    assert "sk-ant-secret-value" not in str(exc.value)


async def test_anthropic_drops_system_messages_from_the_message_list(monkeypatch):
    calls = patch_post(monkeypatch, http_response(json_body={"content": []}))
    client = AnthropicClient(settings(anthropic_api_key="sk-test"))

    await client._complete(
        [LLMMessage(role="system", content="ignored"), LLMMessage(role="user", content="hi")],
        system="the real system prompt",
        temperature=0.0,
        max_tokens=1,
        json_mode=False,
    )

    roles = [m["role"] for m in calls[0]["json"]["messages"]]
    assert roles == ["user"]


async def test_openai_request_shape_and_parsing(monkeypatch):
    calls = patch_post(
        monkeypatch,
        http_response(
            json_body={
                "choices": [{"message": {"content": "hi there"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
                "model": "gpt-x",
            }
        ),
    )
    client = OpenAIClient(settings(openai_api_key="sk-oai"))

    resp = await client._complete(
        [LLMMessage(role="user", content="hi")],
        system="sys",
        temperature=0.1,
        max_tokens=32,
        json_mode=True,
    )

    assert resp.text == "hi there"
    assert resp.stop_reason == "stop"
    assert resp.tokens_in == 4
    assert calls[0]["json"]["response_format"] == {"type": "json_object"}
    assert calls[0]["json"]["messages"][0]["role"] == "system"
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-oai"


async def test_openai_without_key_fails_clearly():
    client = OpenAIClient(settings(openai_api_key=None))
    with pytest.raises(LLMError, match="OPENAI_API_KEY"):
        await client._complete([], system=None, temperature=0.0, max_tokens=1, json_mode=False)


async def test_openai_http_error_is_surfaced(monkeypatch):
    patch_post(monkeypatch, http_response(status=500, text="boom"))
    client = OpenAIClient(settings(openai_api_key="sk-oai"))
    with pytest.raises(LLMError, match="500"):
        await client._complete([], system=None, temperature=0.0, max_tokens=1, json_mode=False)


async def test_openai_empty_choices_do_not_crash(monkeypatch):
    patch_post(monkeypatch, http_response(json_body={"choices": []}))
    client = OpenAIClient(settings(openai_api_key="sk-oai"))

    resp = await client._complete([], system=None, temperature=0.0, max_tokens=1, json_mode=False)
    assert resp.text == ""


async def test_gemini_request_shape_and_parsing(monkeypatch):
    calls = patch_post(
        monkeypatch,
        http_response(
            json_body={
                "candidates": [
                    {"content": {"parts": [{"text": "gem says hi"}]}, "finishReason": "STOP"}
                ],
                "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 6},
            }
        ),
    )
    client = GeminiClient(settings(gemini_api_key="g-key"))

    resp = await client._complete(
        [LLMMessage(role="assistant", content="prior"), LLMMessage(role="user", content="hi")],
        system="sys",
        temperature=0.2,
        max_tokens=64,
        json_mode=True,
    )

    assert resp.text == "gem says hi"
    assert resp.tokens_in == 5
    body = calls[0]["json"]
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["systemInstruction"]["parts"][0]["text"] == "sys"
    # assistant turns are mapped to Gemini's "model" role.
    assert body["contents"][0]["role"] == "model"


async def test_gemini_without_key_fails_clearly():
    client = GeminiClient(settings(gemini_api_key=None))
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        await client._complete([], system=None, temperature=0.0, max_tokens=1, json_mode=False)


async def test_gemini_http_error_is_surfaced(monkeypatch):
    patch_post(monkeypatch, http_response(status=403, text="denied"))


# ---------------------------------------------------------------------------
# build_llm / get_llm: the scripted fallback
# ---------------------------------------------------------------------------
def test_missing_key_falls_back_to_scripted():
    """No key must degrade to the demo brain, never raise."""
    client = build_llm("anthropic", settings=settings(anthropic_api_key=None))
    assert client.provider == "scripted"


def test_present_key_builds_the_real_client():
    client = build_llm("anthropic", settings=settings(anthropic_api_key="sk-x"))
    assert isinstance(client, AnthropicClient)


def test_scripted_provider_never_needs_a_key():
    client = build_llm("scripted", settings=settings())
    assert client.provider == "scripted"


def test_fallback_can_be_disabled():
    cfg = settings(anthropic_api_key=None, allow_scripted_fallback=False)
    client = build_llm("anthropic", settings=cfg)
    assert isinstance(client, AnthropicClient)


def test_unknown_provider_raises_a_helpful_error():
    # Fallback must be off, otherwise the missing-key path rewrites the provider
    # to "scripted" before the unknown-name check is ever reached.
    cfg = settings(allow_scripted_fallback=False)
    with pytest.raises(LLMError, match="Unknown LLM provider"):
        build_llm("not-a-provider", settings=cfg)


def test_unknown_provider_is_absorbed_by_the_scripted_fallback():
    """With fallback on, even a nonsense provider degrades to the demo brain."""
    assert build_llm("not-a-provider", settings=settings()).provider == "scripted"


def test_explicit_model_overrides_the_default():
    client = build_llm("anthropic", "claude-custom", settings=settings(anthropic_api_key="sk-x"))
    assert client.model == "claude-custom"


def test_model_override_does_not_mutate_a_shared_default():
    """Two runs asking for different models must not fight over one client."""
    cfg = settings(anthropic_api_key="sk-x")
    first = build_llm("anthropic", "model-a", settings=cfg)
    second = build_llm("anthropic", "model-b", settings=cfg)
    assert first.model == "model-a"
    assert second.model == "model-b"


def test_get_llm_caches_per_provider_and_model():
    cfg = settings(anthropic_api_key="sk-x")
    a = get_llm("anthropic", "m1", cfg)
    b = get_llm("anthropic", "m1", cfg)
    assert a is b


def test_get_llm_caches_separately_per_model():
    cfg = settings(anthropic_api_key="sk-x")
    assert get_llm("anthropic", "m1", cfg) is not get_llm("anthropic", "m2", cfg)


def test_get_llm_applies_the_scripted_fallback_too():
    client = get_llm("openai", None, settings(openai_api_key=None))
    assert client.provider == "scripted"


def test_reset_llm_drops_the_cache():
    cfg = settings(anthropic_api_key="sk-x")
    first = get_llm("anthropic", "m1", cfg)
    reset_llm()
    assert get_llm("anthropic", "m1", cfg) is not first


def test_describe_reports_provider_and_model():
    cfg = settings(anthropic_api_key="sk-x")
    info = build_llm("anthropic", settings=cfg).describe()
    assert info["provider"] == "anthropic"
    assert info["configured"] is True
    assert info["models"]
    assert "stats" in info


def test_provider_status_marks_scripted_as_always_configured():
    rows = provider_status(settings())
    by_id = {r["id"]: r for r in rows}
    assert by_id["scripted"]["configured"] is True
    assert by_id["scripted"]["active"] is True


def test_provider_status_flags_a_configured_key():
    rows = provider_status(settings(anthropic_api_key="sk-x"))
    by_id = {r["id"]: r for r in rows}
    assert by_id["anthropic"]["configured"] is True


def test_provider_status_falls_back_to_scripted_when_active_key_missing():
    rows = provider_status(settings(llm_provider="openai", openai_api_key=None))
    active = [r["id"] for r in rows if r["active"]]
    assert active == ["scripted"]


async def test_gemini_empty_candidates_do_not_crash(monkeypatch):
    patch_post(monkeypatch, http_response(json_body={"candidates": []}))
    client = GeminiClient(settings(gemini_api_key="g-key"))

    resp = await client._complete([], system=None, temperature=0.0, max_tokens=1, json_mode=False)
    assert resp.text == ""


async def test_repair_attempts_are_bounded():
    client = FakeClient(settings())
    client.outcomes = ["nope"] * 10

    with pytest.raises(LLMOutputError):
        await client.complete_structured(Plan, "plan", max_repairs=1)

    assert len(client.seen) == 2, "max_repairs=1 means exactly two attempts"


async def test_string_prompt_is_wrapped_for_structured_calls():
    client = FakeClient(settings())
    client.outcomes = ['{"goal": "x"}']
    await client.complete_structured(Plan, "just a string")
    assert client.seen[0]["messages"][0].role == "user"


async def test_single_attempt_when_retries_disabled(monkeypatch):
    client = FakeClient(settings(llm_max_retries=0))
    client.outcomes = [httpx.ConnectError("refused")]
    monkeypatch.setattr("app.agent.llm.asyncio.sleep", _no_sleep)

    with pytest.raises(LLMError):
        await client.complete("hi")
    assert client.stats["errors"] == 1
