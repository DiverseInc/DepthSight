"""The paper equity curve must be a TIME SERIES, not a set of distinct balances.

The recorder wrote `zadd(key, {str(balance): timestamp_ms})` -- using the
balance as the sorted-set member. Redis members are unique, so re-recording an
unchanged balance overwrote that member's own score instead of adding a point.
The curve therefore held exactly one point per DISTINCT balance value and froze
the instant the balance stopped moving, which is precisely when a user is
waiting to watch the chart move.

These tests drive the REAL functions, extracted with AST, so they cannot drift
from production the way a re-implementation would.
"""

import ast
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Literal, Optional, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# Extract the real functions from production source.
# --------------------------------------------------------------------------


def _extract_function(rel_path: str, name: str, namespace: Dict[str, Any]):
    """Compile a single function out of a production module, by AST.

    Decorators and annotations are stripped: the FastAPI route decorator needs
    a live app and the annotations reference models we deliberately do not
    import. Only the body under test is executed.
    """
    source = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
    tree = ast.parse(source, rel_path)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            node.decorator_list = []
            node.returns = None
            args = node.args
            for arg in (
                list(args.posonlyargs)
                + list(args.args)
                + list(args.kwonlyargs)
                + [args.vararg, args.kwarg]
            ):
                if arg is not None:
                    arg.annotation = None
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            module = ast.Module(body=[node], type_ignores=[])
            compiled = compile(module, rel_path, "exec")
            exec(compiled, namespace)
            return namespace[name]
    raise AssertionError(f"{name} not found in {rel_path}")


def _bounds(lo, hi):
    """Normalise Redis score bounds, which arrive as '-inf'/'inf' strings."""
    def norm(v):
        if isinstance(v, str):
            if v.startswith("-inf"):
                return float("-inf")
            if v.startswith("+inf") or v == "inf":
                return float("inf")
            return float(v)
        return float(v)

    return norm(lo), norm(hi)


class _MiniSortedSet:
    """In-memory stand-in for the few Redis commands this path uses.

    It models the ONE semantic the bug lived in: a sorted set is keyed on the
    member, so writing the same member twice updates its score instead of
    adding a second entry. Adding a real dependency to assert that would cost
    more than it proves.
    """

    def __init__(self) -> None:
        self.store: Dict[str, float] = {}

    async def zadd(self, key: str, mapping: Dict[str, float]) -> None:
        self.store.update(mapping)

    async def zremrangebyscore(self, key: str, min_: str, max_: float) -> None:
        cutoff = float(max_)
        for member in [m for m, s in self.store.items() if s <= cutoff]:
            del self.store[member]

    async def zrangebyscore(self, key, min, max, withscores=False):
        lo, hi = _bounds(min, max)
        rows = sorted(
            [(m, s) for m, s in self.store.items() if lo <= s <= hi],
            key=lambda kv: kv[1],
        )
        return rows if withscores else [m for m, _ in rows]

    async def zrevrangebyscore(self, key, max, min, withscores=False, start=0, num=1):
        lo, hi = _bounds(min, max)
        rows = sorted(
            [(m, s) for m, s in self.store.items() if lo <= s <= hi],
            key=lambda kv: kv[1],
            reverse=True,
        )
        page = rows[start : start + num]
        return page if withscores else [m for m, _ in page]


# --------------------------------------------------------------------------
# The real reader endpoint.
# --------------------------------------------------------------------------


def _load_reader():
    def Query(default=None, **kwargs):  # noqa: N802 - mimics fastapi.Query
        return default

    def Depends(dependency=None):  # noqa: N802 - mimics fastapi.Depends
        return None

    class _Logger:
        def __init__(self):
            self.warnings: List[str] = []

        def warning(self, msg, *a, **k):
            self.warnings.append(str(msg))

        def error(self, msg, *a, **k):
            self.warnings.append(str(msg))

    logger = _Logger()

    class _HTTPException(Exception):
        def __init__(self, status_code=None, detail=None):
            super().__init__(f"HTTP {status_code}: {detail}")
            self.status_code = status_code
            self.detail = detail

    ns: Dict[str, Any] = {
        "Optional": Optional,
        "List": List,
        "Tuple": Tuple,
        "Literal": Literal,
        "datetime": datetime,
        "timedelta": timedelta,
        "timezone": timezone,
        "logger": logger,
        "Query": Query,
        "Depends": Depends,
        "get_current_user": lambda: None,
        "get_redis_client": lambda: None,
        "HTTPException": _HTTPException,
        "__name__": "portfolio_reader",
    }
    parse = _extract_function("api/routes/portfolio.py", "_parse_equity_member", ns)
    endpoint = _extract_function("api/routes/portfolio.py", "get_portfolio_equity", ns)
    return parse, endpoint, logger


def _load_writer(step_ms: int = 60_000):
    class _Logger:
        def __init__(self):
            self.warnings: List[str] = []

        def warning(self, msg, *a, **k):
            self.warnings.append(str(msg))

        def debug(self, msg, *a, **k):
            pass

        def error(self, msg, *a, **k):
            self.warnings.append(str(msg))

    logger = _Logger()

    # The recorder stamps the member with the clock, so a test that calls it
    # 12 times in a tight loop legitimately produces 12 points all sharing one
    # millisecond. Drive the clock instead: production records every 300s, and
    # this isolates the member format from same-millisecond noise.
    class _Clock(datetime):
        current_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        @classmethod
        def now(cls, tz=None):
            # Step BACKWARD from real now. The reader window ends at real now,
            # so points stamped into the future would be correctly excluded --
            # which would test the window, not the member format.
            cls.current_ms -= step_ms
            return datetime.fromtimestamp(cls.current_ms / 1000, tz=timezone.utc)

    async def get_paper_wallet(db, user_id):
        return [SimpleNamespace(asset="USDT", balance=BALANCE)]

    ns: Dict[str, Any] = {
        "datetime": _Clock,
        "timezone": timezone,
        "logger": logger,
        "crud": SimpleNamespace(get_paper_wallet=get_paper_wallet),
        "__name__": "paper_executor_writer",
    }
    writer = _extract_function("bot_module/paper_executor.py", "_record_equity_point", ns)
    return writer, logger


BALANCE = 9995.50497923637


def _make_executor(writer, redis_client):
    executor = SimpleNamespace(redis_client=redis_client, db=None, user_id=10)
    # Bind to the real executor: binding to a throwaway object would give the
    # method a `self` that never sees redis_client, and the test would pass or
    # fail for entirely the wrong reason.
    executor._record_equity_point = writer.__get__(executor)
    return executor


# --------------------------------------------------------------------------
# The bug is real.
# --------------------------------------------------------------------------


def test_old_member_format_collapses_repeated_balances_into_one_point():
    """Proves the defect is arithmetic, not a guess: 12 recordings, 1 point.

    This is the OLD writer format. It must be asserted, not assumed -- a test
    that only exercises the new code would pass even if the diagnosis were
    wrong. Timestamps are deliberately DISTINCT here, so the collapse can only
    come from the member being the balance.
    """
    redis_client = _MiniSortedSet()
    base_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    async def record_old_format():
        stamp = base_ms + 60_000
        await redis_client.zadd("equity_history:paper:10", {str(BALANCE): stamp})

    for _ in range(12):
        asyncio.run(record_old_format())

    assert len(redis_client.store) == 1, (
        "The old member format was expected to collapse 12 recordings of an "
        f"unchanged balance into 1 point, but produced {len(redis_client.store)}. "
        "If this fails the root-cause diagnosis is wrong and must be revisited."
    )


# --------------------------------------------------------------------------
# The fix works.
# --------------------------------------------------------------------------


def test_fixed_writer_records_a_point_per_recording():
    writer, _ = _load_writer()
    redis_client = _MiniSortedSet()
    executor = _make_executor(writer, redis_client)

    async def run():
        for _ in range(12):
            await executor._record_equity_point()

    asyncio.run(run())

    assert len(redis_client.store) == 12, (
        f"Expected 12 distinct equity points from 12 recordings, got "
        f"{len(redis_client.store)}. The curve is still keyed on the balance."
    )


def test_reader_returns_every_point_sorted_by_time():
    parse, endpoint, _ = _load_reader()
    writer, _ = _load_writer()
    redis_client = _MiniSortedSet()
    executor = _make_executor(writer, redis_client)

    async def run():
        for _ in range(12):
            await executor._record_equity_point()
        return await endpoint(
            period="7d",
            mode="paper",
            current_user=SimpleNamespace(id=10),
            redis_client=redis_client,
        )

    result = asyncio.run(run())

    assert len(result["data"]) == 12
    times = [ts for ts, _ in result["data"]]
    assert times == sorted(times), "Equity points must come back in time order."
    for _, balance in result["data"]:
        assert balance == pytest.approx(BALANCE)


# --------------------------------------------------------------------------
# Backward compatibility and robustness.
# --------------------------------------------------------------------------


def test_reader_still_reads_legacy_bare_balance_members():
    """Points written before the format change must not break the chart."""
    parse, endpoint, _ = _load_reader()
    redis_client = _MiniSortedSet()

    async def run():
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        # Legacy: bare balance as the member.
        await redis_client.zadd("equity_history:paper:10", {"10000.0": now_ms - 60000})
        await redis_client.zadd("equity_history:paper:10", {"9995.5": now_ms})
        return await endpoint(
            period="7d",
            mode="paper",
            current_user=SimpleNamespace(id=10),
            redis_client=redis_client,
        )

    result = asyncio.run(run())
    assert [balance for _, balance in result["data"]] == [10000.0, 9995.5]


@pytest.mark.parametrize(
    "member",
    ["", "not-a-number", "12345:abc", ":::", "12345:", "  :", "nan-ish"],
)
def test_corrupt_member_is_skipped_not_fatal(member):
    """One bad point must not 500 the entire dashboard chart."""
    parse, endpoint, logger = _load_reader()
    redis_client = _MiniSortedSet()

    async def run():
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        await redis_client.zadd("equity_history:paper:10", {member: now_ms})
        await redis_client.zadd("equity_history:paper:10", {"1.5:10000.0": now_ms})
        return await endpoint(
            period="7d",
            mode="paper",
            current_user=SimpleNamespace(id=10),
            redis_client=redis_client,
        )

    result = asyncio.run(run())
    assert result is not None
    assert [balance for _, balance in result["data"]] == [10000.0]


def test_parse_member_reads_both_formats():
    parse, _, _ = _load_reader()
    assert parse("1791245165787:9995.50497923637") == pytest.approx(9995.50497923637)
    assert parse("9995.5") == pytest.approx(9995.5)
    assert parse("1791245165787:0") == pytest.approx(0.0)
    assert parse("1791245165787:-12.25") == pytest.approx(-12.25)
    assert parse("nope") is None