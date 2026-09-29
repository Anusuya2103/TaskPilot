"""Shared pytest fixtures.

Two decisions make the suite fast *and* honest:

* **Isolated globals.** Every test gets a fresh event bus and tool registry, so
  ordering can never matter.
* **A real MockShop, a real browser, a real gate.** The end-to-end tests drive the
  production code path end to end (Chromium + SQLite + SSE); only the LLM is
  replaced, by the deterministic scripted brain.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

# Environment must be configured *before* app modules are imported.
_TMP = Path(tempfile.mkdtemp(prefix="taskpilot-tests-"))
os.environ.update(
    {
        "TASKPILOT_ENV": "test",
        "TASKPILOT_DATABASE_URL": f"sqlite+aiosqlite:///{_TMP / 'test.db'}",
        "TASKPILOT_LLM_PROVIDER": "scripted",
        "TASKPILOT_LOG_LEVEL": "WARNING",
        "TASKPILOT_LOG_JSON": "false",
        "TASKPILOT_ARTIFACT_DIR": str(_TMP / "artifacts"),
        "TASKPILOT_AUTO_APPROVE_LOW_RISK": "true",
        "TASKPILOT_MCP_ENABLED": "false",
        "TASKPILOT_DOMAIN_ALLOWLIST": "localhost,127.0.0.1,mockshop",
        "TASKPILOT_APPROVAL_TIMEOUT_SECONDS": "8",
        "TASKPILOT_BROWSER_HEADLESS": "true",
        "TASKPILOT_MAX_STEP_ATTEMPTS": "2",
        "TASKPILOT_MAX_REPLANS": "1",
        "TASKPILOT_STEP_TIMEOUT_SECONDS": "60",
    }
)

from app.core.config import get_settings, reset_settings_cache  # noqa: E402
from app.core.db import init_models  # noqa: E402
from app.core.events import get_bus, reset_bus  # noqa: E402
from app.tools import register_builtin_tools, reset_builtins, reset_registry  # noqa: E402
from app.tools.registry import get_registry  # noqa: E402
from app.tools.browser import reset_browser_manager  # noqa: E402


@pytest.fixture(scope="session")
def event_loop_policy() -> Any:  # noqa: D103
    return asyncio.get_event_loop_policy()


@pytest.fixture(scope="session", autouse=True)
def _schema() -> Iterator[None]:
    """Create the schema once for the whole session."""
    asyncio.run(init_models())
    yield


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    """Reset process-global singletons between tests."""
    reset_bus()
    reset_registry()
    reset_builtins()
    register_builtin_tools()
    yield
    reset_bus()
    reset_registry()
    reset_builtins()
    reset_browser_manager()


@pytest.fixture
def settings() -> Any:  # noqa: D103
    return get_settings()


@pytest.fixture
def registry():  # noqa: D103
    return get_registry()


@pytest.fixture
def broker() -> Any:
    from app.agent.approvals import ApprovalBroker, reset_broker

    reset_broker()
    return ApprovalBroker(poll_interval=0.05)


@pytest.fixture
def planner() -> Any:
    """Planner backed by the deterministic scripted brain."""
    from app.agent.planner import Planner
    from app.agent.scripted import ScriptedLLM

    return Planner(ScriptedLLM(), None, get_settings())


@pytest.fixture(scope="session")
def mockshop() -> Iterator[Any]:
    """MockShop on a random port, shared by the session; reset before each test."""
    from app.demo.mockshop_runner import start_mockshop

    server = start_mockshop(port=0)
    reset_settings_cache()
    os.environ["TASKPILOT_MOCKSHOP_BASE_URL"] = server.base_url
    reset_settings_cache()
    server.reset()
    yield server
    server.stop()


@pytest.fixture
def mockshop_url(mockshop: Any) -> str:
    mockshop.reset()
    return mockshop.base_url


@pytest.fixture
def captured_events() -> Iterator[list[Any]]:
    """Spy on published events so tests can assert ordering and payloads."""
    bus = get_bus()
    collected: list[Any] = []
    original = bus.publish

    async def spy(record: Any) -> int:
        collected.append(record)
        return await original(record)

    bus.publish = spy  # type: ignore[method-assign]
    yield collected
    bus.publish = original  # type: ignore[method-assign]


@pytest.fixture
async def client() -> AsyncIterator[Any]:
    """httpx client bound to the app (lifespan handled by the caller)."""
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", timeout=30
    ) as http_client:
        yield http_client


@pytest.fixture
async def app_client() -> AsyncIterator[Any]:
    """An httpx client with the app's lifespan actually started."""
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", timeout=60
    ) as http_client:
        # Drive lifespan manually (ASGITransport does not do it for us).
        async with app.router.lifespan_context(app):
            yield http_client
