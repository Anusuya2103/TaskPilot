"""Structured JSON logging with per-run correlation ids.

``contextvars`` keeps this async-safe: the executor binds ``run_id`` and every
log record produced downstream (tools, browser, verifier) carries it.

Secrets are scrubbed structurally *and* textually before anything is emitted,
because tool arguments routinely contain api keys that users pasted in.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import time
from typing import Any

run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("run_id", default=None)
step_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("step_id", default=None)

# --- secret scrubbing --------------------------------------------------------
_SENSITIVE_KEYS = re.compile(
    r"(api[_-]?key|secret|token|password|passwd|pwd|authorization|auth|bearer|cookie|"
    r"session[_-]?id|credit[_-]?card|card[_-]?number|cvv|ssn|private[_-]?key)",
    re.IGNORECASE,
)
_VALUE_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{10,}"),
)
REDACTED = "***redacted***"
MAX_STRING = 4000


def scrub(value: Any, *, depth: int = 0) -> Any:
    """Recursively redact secrets and truncate giant strings."""
    if depth > 8:
        return "<max-depth>"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            out[str(k)] = REDACTED if _SENSITIVE_KEYS.search(str(k)) else scrub(v, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [scrub(v, depth=depth + 1) for v in value][:200]
    if isinstance(value, str):
        text = value
        for pat in _VALUE_PATTERNS:
            text = pat.sub(REDACTED, text)
        if len(text) > MAX_STRING:
            text = text[:MAX_STRING] + f"...<truncated {len(value) - MAX_STRING} chars>"
        return text
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return scrub(str(value), depth=depth + 1)


class JsonFormatter(logging.Formatter):
    """One JSON object per line — ready for Loki / Datadog / CloudWatch."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": round(time.time(), 3),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = run_id_var.get()
        sid = step_id_var.get()
        if rid:
            payload["run_id"] = rid
        if sid:
            payload["step_id"] = sid
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(scrub(extra))
        for key in ("event", "tool", "duration_ms"):
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = scrub(val)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class HumanFormatter(logging.Formatter):
    """Readable console output for local development."""

    COLORS = {
        "DEBUG": "\033[38;5;245m",
        "INFO": "\033[38;5;39m",
        "WARNING": "\033[38;5;214m",
        "ERROR": "\033[38;5;203m",
        "CRITICAL": "\033[48;5;203m\033[38;5;231m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        rid = run_id_var.get()
        prefix = f"[{rid[:8]}] " if rid else ""
        color = self.COLORS.get(record.levelname, "")
        sid = step_id_var.get()
        step = f" ({sid})" if sid else ""
        base = (
            f"{color}{record.levelname:<8}{self.RESET} "
            f"{record.name.split('.')[-1]:<14} {prefix}{record.getMessage()}{step}"
        )
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict) and extra:
            base += " " + json.dumps(scrub(extra), default=str)
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


class _SecretFilter(logging.Filter):
    """Last line of defence: scrub secrets that leak into messages/args."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            if isinstance(record.msg, str):
                record.msg = scrub(record.msg)
            if record.args:
                record.args = tuple(scrub(a) for a in record.args)  # type: ignore[assignment]
        except Exception:  # pragma: no cover - never break logging
            pass
        return True


_configured = False


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    """Idempotent root-logger configuration."""
    global _configured
    root = logging.getLogger()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else HumanFormatter())
    root.handlers = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for noisy in (
        "httpx",
        "httpcore",
        "urllib3",
        "asyncio",
        "sqlalchemy.engine",
        "mcp",
        "playwright",
        "websockets",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if not _configured:
        root.addFilter(_SecretFilter())
        _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def bind_run(run_id: str | None, step_id: str | None = None) -> None:
    run_id_var.set(run_id)
    step_id_var.set(step_id)


def bind_step(step_id: str | None) -> None:
    step_id_var.set(step_id)


def log_event(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    """``logger.log`` with structured, scrubbed fields attached."""
    logger.log(level, msg, extra={"extra_fields": fields})


__all__ = [
    "HumanFormatter",
    "JsonFormatter",
    "bind_run",
    "bind_step",
    "configure_logging",
    "get_logger",
    "log_event",
    "run_id_var",
    "scrub",
    "step_id_var",
]
