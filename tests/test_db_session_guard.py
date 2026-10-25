# tests/test_db_session_guard.py
# ruff: noqa: E402
"""The guard that stops the abandoned-session defect from coming back.

`api.database.get_db` is an async generator, and taking a session out of it
without closing the generator leaves the `async with AsyncSessionLocal()`
suspended at its `yield`: the session keeps its pooled connection checked
out and its transaction OPEN until the garbage collector finalises it. At
fleet scale that starves the pooler - upstream measured ~120 pinned server
connections at 660 controllers.

The rule lives in `tools/db_session_guard.py`. It matches the SHAPE of the
acquisition, not the NAME of the factory, so renaming `get_db_session` to
`_db_session` cannot hide a leak, and it covers both acquisition forms
(`async for` over the generator, and `await anext(gen)`).

These tests check the rule itself against synthetic sources, so a rule that
silently stops matching is caught here rather than at the next incident.
"""

import os
from pathlib import Path

os.environ.setdefault("POSTGRES_USER", "testuser")
os.environ.setdefault("POSTGRES_PASSWORD", "testpassword")
os.environ.setdefault("POSTGRES_DB", "testdb")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "5432")

from tools.db_session_guard import (  # noqa: E402
    ALLOWED,
    REPO,
    find_offenders,
    find_offenders_in_source,
    scanned_files,
)

# A leak written the way the code used to write it.
LEAK_ASYNC_FOR = """
async def handler(self):
    async for db in self.get_db_session():
        config = await crud.get_config(db, user_id=self.user_id)
        await db.commit()
"""

# The SAME leak with the factory renamed. The rule must not depend on the
# name - this is the GAP 3 mutant.
LEAK_RENAMED_FACTORY = """
async def handler(self):
    async for db in self._db_session():
        config = await crud.get_config(db, user_id=self.user_id)
        await db.commit()
"""

# The assign-then-iterate form: the factory call is not in the `async for`.
LEAK_ASSIGN_THEN_ITERATE = """
async def handler(self):
    sessions = self.get_db_session()
    async for db in sessions:
        config = await crud.get_config(db, user_id=self.user_id)
"""

# The `anext` acquisition form, never closed.
LEAK_UNCLOSED_ANEXT = """
async def handler(self):
    db_gen = get_db()
    db = await anext(db_gen)
    config = await crud.get_config(db, user_id=self.user_id)
"""

# The `anext` acquisition form, closed.
CLOSED_ANEXT = """
async def handler(self):
    db_gen = get_db()
    db = await anext(db_gen)
    try:
        config = await crud.get_config(db, user_id=self.user_id)
    finally:
        await db_gen.aclose()
"""

# The correct shape.
OWNED_SESSION = """
async def handler(self):
    async with owned_session(self.get_db_session) as db:
        config = await crud.get_config(db, user_id=self.user_id)
"""

# A loop that has nothing to do with the database.
NOT_A_SESSION = """
async def handler(websocket):
    async for message in websocket:
        await websocket.send_json(message)
"""


def test_the_guard_finds_no_unowned_db_sessions():
    """The whole repo: no acquisition outside the documented leftovers.

    The assertion is `== ALLOWED`, not `not in ALLOWED`: a new offender must
    fail the test, and an allowlist entry that stops matching (because it
    was fixed) must fail it too, so the entry cannot go stale.
    """
    offenders = find_offenders()
    assert offenders, "the guard found nothing at all - it is not running"
    allowed_hits = {_key(o) for o in offenders if _key(o) in ALLOWED}
    unexpected = [o for o in offenders if _key(o) not in ALLOWED]
    assert unexpected == [], f"unowned DB sessions: {unexpected}"
    assert allowed_hits == set(ALLOWED), (
        "the allowlist is stale: "
        f"expected {sorted(ALLOWED)}, the guard matched {sorted(allowed_hits)}"
    )


def test_the_guard_covers_the_repo_root_and_the_api_package():
    """The command listener lives in the repo root; `api/` holds the factory.

    A guard that only scanned `bot_module/` would be blind to
    `bot_runner.py` - which is where the largest leak in this fix was.
    """
    files = {p.relative_to(REPO).as_posix() for p in scanned_files()}
    assert "bot_runner.py" in files, "the repo root is not scanned"
    assert any(f.startswith("api/") for f in files), "api/ is not scanned"
    assert any(f.startswith("bot_module/") for f in files), "bot_module/ is not scanned"


def test_the_allowed_leftover_is_a_real_site():
    """Each allowlist entry must still point at a real line of a real file."""
    for (path, line), reason in ALLOWED.items():
        target = REPO / path
        assert target.is_file(), f"{path} does not exist"
        assert reason.strip(), f"{path}:{line} has no documented reason"
        lines = target.read_text(encoding="utf-8").splitlines()
        assert line <= len(lines), f"{path}:{line} is past the end of the file"


def test_the_rule_flags_an_async_for_over_a_session_factory():
    assert find_offenders_in_source(LEAK_ASYNC_FOR), "the rule missed a leak"


def test_the_rule_flags_a_renamed_factory():
    """GAP 3: the rule must be about the shape, not the name."""
    assert find_offenders_in_source(LEAK_RENAMED_FACTORY), (
        "the rule missed a leak after the factory was renamed"
    )


def test_the_rule_flags_the_assign_then_iterate_form():
    """GAP 2: the factory call is not in the `async for`."""
    assert find_offenders_in_source(LEAK_ASSIGN_THEN_ITERATE), (
        "the rule missed the assign-then-iterate form"
    )


def test_the_rule_flags_an_unclosed_anext_acquisition():
    """GAP 2: the `await anext(gen)` acquisition form."""
    assert find_offenders_in_source(LEAK_UNCLOSED_ANEXT), (
        "the rule missed an unclosed anext acquisition"
    )


def test_the_rule_does_not_flag_a_closed_anext_acquisition():
    assert not find_offenders_in_source(CLOSED_ANEXT), (
        "the rule flagged a generator that IS closed"
    )


def test_the_rule_does_not_flag_an_owned_session():
    assert not find_offenders_in_source(OWNED_SESSION), (
        "the rule flagged the correct `owned_session` shape"
    )


def test_the_rule_does_not_flag_a_loop_that_is_not_a_session():
    assert not find_offenders_in_source(NOT_A_SESSION), (
        "the rule flagged a websocket loop"
    )


def _key(offender: str):
    path, _, line = offender.rpartition(":")
    return path, int(line)
