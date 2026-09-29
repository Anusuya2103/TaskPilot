"""FastAPI application factory.

Wiring worth knowing about:

* **Lifespan** — schema creation, MCP connect (best effort), and *interrupted-run
  recovery*: any run still marked ``running``/``awaiting_approval`` at boot is
  marked failed, because its in-memory task died with the previous process.
* **Error handling** — every :class:`TaskPilotError` becomes a consistent JSON
  body with a stable ``code`` so the frontend can branch on it.
* **Static media** — screenshots and reports are served from ``/media`` and
  ``/reports``, which is what makes the audit trail and the filmstrip work.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.routes import events, health, runs, tasks, tools
from app.core.config import get_settings
from app.core.db import dispose_engine, init_models
from app.core.errors import TaskPilotError
from app.core.logging import configure_logging, get_logger, log_event
from app.core.models import Run, RunStatus
from app.tools import register_builtin_tools

logger = get_logger(__name__)

DESCRIPTION = """
**TaskPilot** plans a task, acts across real web apps, and **pauses for your
approval** before anything risky or irreversible happens.

* `POST /api/tasks` - create a task and start a run
* `GET /api/runs/{id}/events` - live SSE feed (resumable via `Last-Event-ID`)
* `POST /api/runs/{id}/approvals/{step_id}` - approve / reject / edit-and-approve
* `GET /api/runs/{id}/summary` - metrics + approval-gate integrity
"""

# Statuses that imply an in-flight task owned by *this* process.
IN_FLIGHT_STATUSES = (
    RunStatus.RUNNING.value,
    RunStatus.PLANNING.value,
    RunStatus.AWAITING_APPROVAL.value,
    RunStatus.AWAITING_PLAN_APPROVAL.value,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.log_json)
    register_builtin_tools()

    if settings.auto_create_tables:
        try:
            await init_models()
        except Exception as exc:  # noqa: BLE001 - let /ready report it
            logger.exception("schema initialisation failed: %s", exc)

    await _recover_interrupted_runs()

    if settings.mcp_enabled:
        try:
            from app.tools.mcp_bridge import get_mcp_manager
            from app.tools.registry import get_registry

            proxies = await get_mcp_manager().start()
            if proxies:
                get_registry().replace_source("mcp", proxies)
        except Exception as exc:  # noqa: BLE001 - MCP is optional, never fatal
            logger.warning("MCP startup failed (continuing without it): %s", exc)

    log_event(
        logger,
        logging.INFO,
        "TaskPilot API ready",
        version=__version__,
        env=settings.env,
        provider=settings.llm_provider,
    )
    try:
        yield
    finally:
        from app.tools.browser import get_browser_manager
        from app.tools.mcp_bridge import get_mcp_manager

        await get_browser_manager().close_all()
        await get_mcp_manager().stop()
        await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="TaskPilot API",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Response-Time-ms"],
    )

    # --- middleware ----------------------------------------------------------
    @app.middleware("http")
    async def request_context(request: Request, call_next: Any) -> Any:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-ms"] = str(elapsed)
        if request.url.path.startswith("/api") and elapsed > 1500:
            log_event(
                logger,
                logging.WARNING,
                "slow request",
                request_id=request_id,
                path=request.url.path,
                method=request.method,
                duration_ms=elapsed,
            )
        return response

    # --- error handling ------------------------------------------------------
    @app.exception_handler(TaskPilotError)
    async def taskpilot_error_handler(request: Request, exc: TaskPilotError) -> JSONResponse:
        payload: dict[str, Any] = exc.to_dict()
        payload["request_id"] = getattr(request.state, "request_id", None)
        log_event(
            logger,
            logging.WARNING if exc.http_status < 500 else logging.ERROR,
            f"api error: {exc.code}",
            request_id=payload["request_id"],
            path=request.url.path,
            code=exc.code,
        )
        return JSONResponse(status_code=exc.http_status, content={"error": payload})

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        """Never leak an exception message: it can contain data from a page."""
        request_id = getattr(request.state, "request_id", None)
        logger.exception("unhandled error on %s", request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": f"{type(exc).__name__}: an unexpected error occurred",
                    "retryable": True,
                    "details": {},
                    "request_id": request_id,
                }
            },
        )

    # --- routes --------------------------------------------------------------
    app.include_router(health.router)
    app.include_router(tasks.router)
    app.include_router(runs.router)
    app.include_router(events.router)
    app.include_router(tools.router)

    @app.get("/api/config", tags=["meta"], summary="Public client configuration")
    async def public_config() -> dict[str, Any]:
        """Everything the frontend needs to render, with no secrets."""
        return {
            "version": __version__,
            "env": settings.env,
            "llm_provider": settings.llm_provider,
            "llm_model": settings.llm_model,
            "demo_mode": settings.llm_provider == "scripted"
            or (
                not settings.provider_key(settings.llm_provider)
                and settings.allow_scripted_fallback
            ),
            "approval_timeout_seconds": settings.approval_timeout_seconds,
            "auto_approve_low_risk": settings.auto_approve_low_risk,
            "high_risk_always_requires_approval": True,
            "planner_preview": settings.planner_preview,
        }

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {"name": "TaskPilot", "version": __version__, "docs": "/docs"}

    # --- static media --------------------------------------------------------
    screenshots_dir = settings.screenshots_path
    reports_dir = settings.reports_path
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/media/screenshots", StaticFiles(directory=str(screenshots_dir)), name="screenshots")
    app.mount("/reports", StaticFiles(directory=str(reports_dir)), name="reports")

    return app


async def _recover_interrupted_runs() -> None:
    """Fail any run whose in-memory task did not survive a restart.

    A paused run holds an ``asyncio.Task`` in this process, so a redeploy kills
    it. Marking those runs failed with a specific reason is far more honest than
    leaving them ``running`` forever.
    """
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.core.events import append_event, get_bus
    from app.core.models import (
        Approval,
        ApprovalStatus,
        EventType,
        Step,
        StepStatus,
        utcnow,
    )

    try:
        async with session_scope() as session:
            result = await session.execute(
                select(Run).where(Run.status.in_(list(IN_FLIGHT_STATUSES)))
            )
            runs = result.scalars().all()
            for run in runs:
                run.status = RunStatus.FAILED.value
                run.finished_at = utcnow()
                run.error = {
                    "code": "interrupted",
                    "message": (
                        "The API process restarted while this run was in flight. The audit "
                        "trail below is complete; the run itself did not finish."
                    ),
                    "retryable": True,
                }
                steps = await session.execute(
                    select(Step).where(
                        Step.run_id == run.id,
                        Step.status.in_(
                            [StepStatus.RUNNING.value, StepStatus.AWAITING_APPROVAL.value]
                        ),
                    )
                )
                for step in steps.scalars().all():
                    step.status = StepStatus.FAILED.value
                    step.error = {"code": "interrupted", "message": "process restarted"}
                approvals = await session.execute(
                    select(Approval).where(
                        Approval.run_id == run.id,
                        Approval.status == ApprovalStatus.PENDING.value,
                    )
                )
                for approval in approvals.scalars().all():
                    approval.status = ApprovalStatus.CANCELLED.value
                    approval.decided_at = utcnow()
                    approval.decided_by = "system"
                    approval.decision_note = "API process restarted"
                await append_event(
                    session,
                    run.id,
                    EventType.RUN_FAILED,
                    message="Run interrupted by an API restart",
                    payload={"code": "interrupted"},
                )
            if runs:
                log_event(
                    logger, logging.WARNING, "marked interrupted runs as failed", count=len(runs)
                )
        for run in runs:
            get_bus().close_channel(run.id)
    except Exception as exc:  # noqa: BLE001 - recovery is best effort
        logger.warning("run recovery failed: %s", exc)


_app: FastAPI | None = None


def get_app() -> FastAPI:
    """Lazy singleton so ``uvicorn app.main:get_app --factory`` works."""
    global _app
    if _app is None:
        _app = create_app()
    return _app
