"""Security primitives.

Three concerns live here because they all answer "what do we refuse to trust?":

1. **Prompt injection.** Anything scraped from a page or returned by a tool is
   *untrusted data*, never an instruction. :func:`sanitize_untrusted` strips
   instruction-like lines, records what it removed, and wraps the remainder in
   explicit delimiters so the planner/verifier see a clear provenance boundary.
2. **Domain allowlist.** The browser and HTTP tools refuse to leave the
   configured allowlist, which bounds what a hijacked plan can reach.
3. **Rate limiting.** A dependency-free token bucket keyed by client identity.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from app.core.config import Settings, get_settings
from app.core.errors import PolicyViolation

# ---------------------------------------------------------------------------
# 1. prompt injection
# ---------------------------------------------------------------------------
# Patterns that look like an attempt to talk to the model instead of describe
# page content. Ordered by severity; every match is reported.
INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ignore_instructions",
        re.compile(
            r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b"
            r"(previous|prior|above|earlier|all)\b[^.\n]{0,30}\b(instruction|prompt|rule|direction)",
            re.IGNORECASE,
        ),
    ),
    (
        "role_switch",
        re.compile(
            r"(you are now|act as (?:a|an|the)|new (?:system )?(?:prompt|instructions?)|"
            r"system\s*:\s*|assistant\s*:\s*|developer\s*:\s*)",
            re.IGNORECASE,
        ),
    ),
    (
        "secret_exfiltration",
        re.compile(
            r"(reveal|print|show|dump|expose|send)[^.\n]{0,30}"
            r"(api[ _-]?key|secret|token|password|credential|env(?:ironment)? var|\.env)",
            re.IGNORECASE,
        ),
    ),
    (
        "tool_hijack",
        re.compile(
            r"(call|invoke|execute|run)\s+(the\s+)?(tool|function|command|shell|bash|python|curl|\brm\b|\bdel\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "approval_bypass",
        re.compile(
            r"(skip|bypass|ignore|no need for|without)\s+(the\s+)?"
            r"(approval|confirmation|review|permission|authorization|human)",
            re.IGNORECASE,
        ),
    ),
    (
        "payment_redirection",
        re.compile(
            r"(send|transfer|pay|wire)[^.\n]{0,40}(to|into)\s+(this\s+)?"
            r"(account|wallet|address|0x[0-9a-f]{6,})",
            re.IGNORECASE,
        ),
    ),
    (
        "exfil_url",
        re.compile(
            r"https?://[^\s\"'<>]*\?[^\s\"'<>]*=(?:\S)*(key|token|secret|password)",
            re.IGNORECASE,
        ),
    ),
    (
        "instruction_markers",
        re.compile(
            r"(^|\n)\s*(?:#{2,}\s*)?(instructions?|system|important|"
            r"note to (?:the )?(?:ai|assistant|agent))\s*:",
            re.IGNORECASE,
        ),
    ),
)

INVISIBLE_CHARS = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff\u00ad]")
ZERO_WIDTH_PAYLOAD = re.compile("[\u200b\u200c\u200d]{4,}")

UNTRUSTED_OPEN = "<<<UNTRUSTED_DATA"
UNTRUSTED_CLOSE = "UNTRUSTED_DATA>>>"
MAX_OBSERVATION_CHARS = 6000


@dataclass(slots=True)
class SanitizedText:
    """Result of passing untrusted content through the sanitizer."""

    text: str
    flags: list[str] = field(default_factory=list)
    removed_lines: int = 0
    truncated: bool = False

    @property
    def suspicious(self) -> bool:
        return bool(self.flags)

    def as_prompt_block(self, label: str = "untrusted") -> str:
        return f"{UNTRUSTED_OPEN} source={label}\n{self.text}\n{UNTRUSTED_CLOSE}"


def detect_injection(text: str) -> list[str]:
    """Return the names of every injection pattern that matched."""
    if not text:
        return []
    flags = [name for name, pattern in INJECTION_PATTERNS if pattern.search(text)]
    if ZERO_WIDTH_PAYLOAD.search(text):
        flags.append("zero_width_payload")
    return flags


def lexical_injection_score(text: str) -> float:
    """Crude 0..1 score used for logging/telemetry (not for gating)."""
    flags = detect_injection(text)
    return min(1.0, 0.34 + 0.11 * len(flags)) if flags else 0.0


def sanitize_untrusted(
    text: str | None,
    *,
    max_chars: int = MAX_OBSERVATION_CHARS,
    keep_suspicious: bool = False,
) -> SanitizedText:
    """Neutralise *content* so it can never masquerade as *instructions*.

    ``keep_suspicious=False`` (the default for anything fed back to the model)
    drops offending lines entirely and reports the count. Set it to ``True`` when
    a human needs to see the raw text in the audit view.
    """
    if not text:
        return SanitizedText(text="")
    cleaned = INVISIBLE_CHARS.sub("", text)
    truncated = len(cleaned) > max_chars
    if truncated:
        cleaned = cleaned[:max_chars] + "\n...[truncated]"

    flags = detect_injection(cleaned)
    removed = 0
    if flags and not keep_suspicious:
        kept: list[str] = []
        for raw_line in cleaned.splitlines():
            line_flags = detect_injection(raw_line)
            if line_flags:
                removed += 1
                kept.append(
                    f"[line removed by TaskPilot safety filter: {', '.join(line_flags)}]"
                )
                continue
            kept.append(raw_line)
        cleaned = "\n".join(kept)

    return SanitizedText(
        text=cleaned, flags=flags, removed_lines=removed, truncated=truncated
    )


def wrap_untrusted(payload: str, label: str) -> str:
    """Delimit untrusted content for prompts (defence in depth, layer 2)."""
    return SanitizedText(text=payload).as_prompt_block(label)


# ---------------------------------------------------------------------------
# 2. domain allowlist
# ---------------------------------------------------------------------------
def hostname_of(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return (parsed.hostname or "").lower()


def _is_private_host(host: str) -> bool:
    if host in {"localhost", ""} or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


def host_matches(host: str, patterns: Iterable[str]) -> bool:
    """Glob-aware match supporting ``*.example.com`` and a ``*`` catch-all."""
    host = host.lower()
    for raw in patterns:
        pattern = raw.lower().strip()
        if not pattern:
            continue
        if pattern == "*":
            return True
        if pattern.startswith("*."):
            base = pattern[2:]
            if host == base or host.endswith("." + base):
                return True
            continue
        if fnmatch.fnmatch(host, pattern):
            return True
    return False


def check_url_allowed(url: str, settings: Settings | None = None) -> str:
    """Raise :class:`PolicyViolation` if ``url`` is outside the allowlist."""
    settings = settings or get_settings()
    host = hostname_of(url)
    if not host:
        raise PolicyViolation(f"Could not parse a hostname from {url!r}")
    if settings.block_private_networks and _is_private_host(host):
        raise PolicyViolation(
            f"Host {host!r} resolves to a private network and private networks are blocked",
            details={"host": host, "url": url},
        )
    if not settings.allowlist_patterns:
        return host
    if not host_matches(host, settings.allowlist_patterns):
        raise PolicyViolation(
            f"Domain {host!r} is not on the allowlist. Add it in Settings to allow it.",
            details={"host": host, "url": url, "allowlist": settings.allowlist_patterns},
        )
    return host


def is_url_allowed(url: str, settings: Settings | None = None) -> bool:
    try:
        check_url_allowed(url, settings)
        return True
    except PolicyViolation:
        return False


# ---------------------------------------------------------------------------
# 3. rate limiting (dependency-free rolling window)
# ---------------------------------------------------------------------------
class TokenBucket:
    """Rolling-window counter. Cheap, per-process, good enough for a demo + CI."""

    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, list[float]] = {}

    def check(self, key: str, *, now: float | None = None) -> tuple[bool, int]:
        """Return ``(allowed, remaining)``."""
        if self.limit <= 0:
            return True, -1
        now = now if now is not None else time.monotonic()
        cutoff = now - self.window
        bucket = [t for t in self._hits.get(key, ()) if t > cutoff]
        if len(bucket) >= self.limit:
            self._hits[key] = bucket
            return False, 0
        bucket.append(now)
        self._hits[key] = bucket
        if len(self._hits) > 5000:  # pragma: no cover - memory guard
            self._hits = {k: v for k, v in self._hits.items() if v and v[-1] > cutoff}
        return True, self.limit - len(bucket)

    def reset(self) -> None:
        self._hits.clear()


_limiter: TokenBucket | None = None


def get_limiter() -> TokenBucket:
    global _limiter
    if _limiter is None:
        _limiter = TokenBucket(get_settings().rate_limit_per_minute)
    return _limiter


def reset_limiter() -> None:
    global _limiter
    _limiter = None


def safe_preview(value: Any, limit: int = 300) -> str:
    """Short, log-safe rendering of arbitrary tool args."""
    from app.core.logging import scrub

    text = str(scrub(value))
    return text if len(text) <= limit else text[:limit] + "…"


__all__ = [
    "INJECTION_PATTERNS",
    "MAX_OBSERVATION_CHARS",
    "SanitizedText",
    "TokenBucket",
    "UNTRUSTED_CLOSE",
    "UNTRUSTED_OPEN",
    "check_url_allowed",
    "detect_injection",
    "get_limiter",
    "host_matches",
    "hostname_of",
    "is_url_allowed",
    "lexical_injection_score",
    "reset_limiter",
    "safe_preview",
    "sanitize_untrusted",
    "wrap_untrusted",
]
