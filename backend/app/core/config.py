"""Application settings.

Every knob is environment driven so the same image runs locally, in CI, in
docker-compose and on Render/Fly.  Nothing here may import from ``app.tools`` or
``app.agent`` — config is the bottom of the dependency graph.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(__file__).resolve().parents[2]

LLMProvider = Literal["anthropic", "openai", "gemini", "scripted"]


class Settings(BaseSettings):
    """Runtime configuration. See ``.env.example`` for documentation."""

    model_config = SettingsConfigDict(
        env_prefix="TASKPILOT_",
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- core -----------------------------------------------------------------
    env: str = "development"
    log_level: str = "INFO"
    log_json: bool = True
    api_key: str | None = None
    public_api_url: str = "http://localhost:8000"
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    # requests/minute per client IP (0 disables)
    rate_limit_per_minute: int = 240

    # --- database -------------------------------------------------------------
    database_url: str = Field(
        default="sqlite+aiosqlite:///./taskpilot.db",
        validation_alias=AliasChoices("TASKPILOT_DATABASE_URL", "DATABASE_URL", "database_url"),
    )
    auto_create_tables: bool = True
    db_echo: bool = False

    # --- llm ------------------------------------------------------------------
    llm_provider: LLMProvider = "anthropic"
    llm_model: str = "claude-sonnet-4-5-20250929"
    llm_base_url: str | None = None
    llm_temperature: float = 0.0
    llm_max_tokens: int = 4096
    llm_timeout_seconds: float = 90.0
    llm_max_retries: int = 3
    allow_scripted_fallback: bool = True
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    gemini_api_key: str | None = None

    # --- agent loop -----------------------------------------------------------
    max_step_attempts: int = 2
    max_replans: int = 2
    max_steps: int = 20
    run_timeout_seconds: int = 900
    step_timeout_seconds: int = 120
    approval_timeout_seconds: int = 300
    auto_approve_low_risk: bool = True
    planner_preview: bool = False  # start runs paused for plan review

    # --- browser --------------------------------------------------------------
    browser_headless: bool = True
    browser_timeout_ms: int = 20000
    browser_slow_mo_ms: int = 0
    artifact_dir: str = "./artifacts"
    domain_allowlist: str = (
        "localhost,127.0.0.1,mockshop,books.toscrape.com,example.com,httpbin.org"
    )
    block_private_networks: bool = False
    max_screenshots_per_run: int = 120

    # --- mcp ------------------------------------------------------------------
    mcp_enabled: bool = True
    mcp_servers: str = "[]"
    mcp_startup_timeout_seconds: float = 25.0
    mcp_call_timeout_seconds: float = 60.0

    # --- demo targets ---------------------------------------------------------
    mockshop_base_url: str = "http://localhost:9000"
    books_base_url: str = "https://books.toscrape.com"

    # --- validators -----------------------------------------------------------
    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @field_validator("database_url")
    @classmethod
    def _normalise_db_url(cls, v: str) -> str:
        """Accept the URL shapes that PaaS providers hand out."""
        v = v.strip()
        if v.startswith("postgres://"):  # Render/Heroku legacy scheme
            v = "postgresql+asyncpg://" + v[len("postgres://") :]
        elif v.startswith("postgresql://"):
            v = "postgresql+asyncpg://" + v[len("postgresql://") :]
        elif v.startswith("sqlite://") and "+aiosqlite" not in v:
            v = "sqlite+aiosqlite://" + v[len("sqlite://") :]
        return v

    @model_validator(mode="after")
    def _pull_provider_keys(self) -> Settings:
        """API keys are conventionally *unprefixed*; the SDKs read them too."""
        if not self.anthropic_api_key:
            self.anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")
        if not self.openai_api_key:
            self.openai_api_key = os.getenv("OPENAI_API_KEY")
        if not self.gemini_api_key:
            self.gemini_api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        return self

    # --- derived --------------------------------------------------------------
    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_production(self) -> bool:
        return self.env.lower() in {"production", "prod"}

    @property
    def cors_origin_list(self) -> list[str]:
        raw = [o.strip() for o in self.cors_origins.split(",") if o.strip()]
        return ["*"] if "*" in raw else raw

    @property
    def allowlist_patterns(self) -> list[str]:
        return [d.strip().lower() for d in self.domain_allowlist.split(",") if d.strip()]

    @property
    def artifact_path(self) -> Path:
        p = Path(self.artifact_dir)
        if not p.is_absolute():
            p = (BACKEND_ROOT / p).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def screenshots_path(self) -> Path:
        p = self.artifact_path / "screenshots"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def reports_path(self) -> Path:
        p = self.artifact_path / "reports"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def mcp_server_configs(self) -> list[dict[str, Any]]:
        """Parse ``TASKPILOT_MCP_SERVERS`` (JSON array, or a path to a JSON file)."""
        raw = (self.mcp_servers or "").strip()
        if not raw or raw == "[]":
            return []
        if not raw.startswith("["):
            candidate = Path(raw)
            if candidate.exists():
                raw = candidate.read_text(encoding="utf-8")
            else:
                raise ValueError("TASKPILOT_MCP_SERVERS must be a JSON array or file path")
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError("TASKPILOT_MCP_SERVERS must be a JSON array")
        return [i for i in data if isinstance(i, dict) and i.get("name")]

    def provider_key(self, provider: str | None = None) -> str | None:
        provider = (provider or self.llm_provider).lower()
        return {
            "anthropic": self.anthropic_api_key,
            "openai": self.openai_api_key,
            "gemini": self.gemini_api_key,
            "scripted": "scripted",
        }.get(provider)

    def redacted(self) -> dict[str, Any]:
        """Settings snapshot safe to log / return to the UI."""
        data = self.model_dump()
        for key in ("anthropic_api_key", "openai_api_key", "gemini_api_key", "api_key"):
            if data.get(key):
                data[key] = "***"
        return data


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Used by tests that mutate the environment."""
    get_settings.cache_clear()


__all__ = [
    "BACKEND_ROOT",
    "REPO_ROOT",
    "LLMProvider",
    "Settings",
    "get_settings",
    "reset_settings_cache",
]
