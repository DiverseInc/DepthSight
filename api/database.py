# api/database.py
import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base
from contextlib import asynccontextmanager

# 1. Read all necessary data from environment variables
#    Variable names must match those used in the .env file
DB_USER = os.environ.get("POSTGRES_USER")
DB_PASS = os.environ.get("POSTGRES_PASSWORD")
DB_HOST = os.environ.get("POSTGRES_HOST")
DB_NAME = os.environ.get("POSTGRES_DB")
DB_PORT = os.environ.get("POSTGRES_PORT", 5432)

# 2. Validation (very important!). Verify all variables are found.
#    If any is missing, the application will fail with a clear error instead of breaking silently.
if not all([DB_USER, DB_PASS, DB_HOST, DB_NAME]):
    raise ValueError("One or more required database environment variables are not set!")

# 3. Assemble connection string from retrieved variables
DATABASE_URL = f"postgresql+asyncpg://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


def alembic_database_url(database_url: str = DATABASE_URL) -> str:
    """The database URL Alembic must use: the same database, SYNC driver.

    WHY THIS EXISTS
    ---------------
    The application talks to PostgreSQL over the ASYNC driver (asyncpg), so
    ``DATABASE_URL`` is ``postgresql+asyncpg://...``.  Alembic runs
    SYNCHRONOUSLY and cannot use asyncpg; it needs psycopg2, which
    ``requirements.txt`` ships (``psycopg2-binary>=2.9,<3.0``).

    The bug this replaces: ``DATABASE_URL.replace("+asyncpg", "")`` strips the
    driver name entirely and leaves the BARE ``postgresql://`` scheme.  On
    SQLAlchemy 2.x a bare ``postgresql://`` is not "no driver" -- it is
    "pick one", and it picks PSYCOPG3, which is not installed.  The result was
    ``ModuleNotFoundError: No module named 'psycopg'`` from
    ``alembic upgrade head``, which ``docker-startup.sh`` swallowed, so the
    API came up serving traffic while the schema never advanced.

    So: name the driver explicitly.  Do not strip it.
    """
    return database_url.replace("+asyncpg", "+psycopg2")


engine = create_async_engine(DATABASE_URL)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

Base = declarative_base()

# Alias for WebSocket server and other components that need a context manager for sessions
async_session_factory = AsyncSessionLocal


def get_session_for_worker() -> async_sessionmaker[AsyncSession]:
    """
    Returns session factory for use in background tasks (Celery).
    This allows session creation in the worker context.
    """
    return AsyncSessionLocal


# DEPENDENCY FOR ENDPOINTS
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


@asynccontextmanager
async def get_isolated_worker_session():
    """
    Creates fully isolated engine and session for a single Celery task.
    Guarantees that all resources (including connection pool) are destroyed after use.
    Used as async context manager (async with).
    """
    # 1. Create a new, temporary engine for this specific task.
    worker_engine = create_async_engine(DATABASE_URL)

    # 2. Create a new session factory bound to this temporary engine.
    WorkerSessionLocal = async_sessionmaker(worker_engine, expire_on_commit=False)

    # 3. Create and yield session.
    async with WorkerSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    # 4. After completing 'with' block, guarantee destruction of engine and connection pool.
    await worker_engine.dispose()
