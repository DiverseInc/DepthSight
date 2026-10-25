# bot_module/db_session.py
"""Owned DB sessions: a session that is always given back to the pool.

`api.database.get_db` is an async generator:

    async def get_db():
        async with AsyncSessionLocal() as session:
            yield session

`async for db in get_db():` walks that generator. When the body returns -
or raises, or `break`s - the generator is left suspended at its `yield`, so
the `async with` never exits: the session keeps its pool connection checked
out and its transaction OPEN until the garbage collector happens to
finalise the generator. That release is not something production can rely
on: upstream measured ~120 pinned server connections at 660 controllers,
which starved the pooler and made every other query queue.

`owned_session` hands the caller the session and guarantees the release:
rolled back when the body raises, closed either way. The connection goes
back to the pool on the happy path AND on the error path.

It works with the real `get_db` and with the async-generator mocks used in
tests - anything that is an async generator yielding one session.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Callable

__all__ = ["owned_session"]


@asynccontextmanager
async def owned_session(db_session_factory: Callable[[], Any]):
    """Yield one DB session that is always rolled back and closed.

    The session is rolled back when the body raises - the caller had not
    committed - and closed either way, so the pool connection is returned
    exactly once. Closing the session is what returns the connection; a
    rollback alone does not.
    """
    gen = db_session_factory()
    try:
        db = await gen.__anext__()
    except StopAsyncIteration:
        raise RuntimeError("DB session factory yielded nothing")
    try:
        yield db
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            await gen.aclose()
        except Exception:
            pass
        try:
            await db.close()
        except Exception:
            pass
