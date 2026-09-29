"""SQLAlchemy 2.0 async engine + session plumbing.

Works against Postgres (``asyncpg``) in docker/production and SQLite
(``aiosqlite``) for local dev, tests and the evaluation harness.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def build_engine(settings: Settings | None = None) -> AsyncEngine:
    """Create an engine. SQLite needs a couple of pragmas for sane concurrency."""
    settings = settings or get_settings()
    kwargs: dict = {"echo": settings.db_echo, "future": True}
    if settings.is_sqlite:
        # `check_same_thread=False` + NullPool-ish behaviour keeps the test
        # client (which uses a thread pool) from tripping over locking.
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        kwargs["poolclass"] = None
    else:
        kwargs.update(pool_size=10, max_overflow=20, pool_pre_ping=True, pool_recycle=1800)
    engine = create_async_engine(settings.database_url, **kwargs)

    if settings.is_sqlite:
        from sqlalchemy import event

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver hook
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

    return engine


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _sessionmaker


async def dispose_engine() -> None:
    """Release the global engine (called from app shutdown / tests)."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope for background tasks (executor, workers).

    Commits on success, rolls back on any exception, always closes.
    """
    factory = get_sessionmaker()
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_models() -> None:
    """Create tables when migrations have not been run (SQLite / first boot)."""
    from app.core.models import Base

    engine = get_engine()
    async with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            from sqlalchemy import text

            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(Base.metadata.create_all)
    logger.info("database schema ready", extra={"extra_fields": {"dialect": engine.dialect.name}})


async def ping() -> bool:
    """Readiness probe: cheap SELECT 1."""
    from sqlalchemy import text

    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # pragma: no cover - depends on infra
        logger.warning("database ping failed", extra={"extra_fields": {"error": str(exc)}})
        return False


__all__ = [
    "build_engine",
    "dispose_engine",
    "get_engine",
    "get_session",
    "get_sessionmaker",
    "init_models",
    "ping",
    "session_scope",
]
