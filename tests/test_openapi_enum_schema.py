# tests/test_openapi_enum_schema.py

"""The published OpenAPI schema must keep advertising `enum` on the query
parameters that used to be declared with the removed `Query(enum=...)` kwarg.

FastAPI 0.156 REMOVED `enum` from `Query()`, so `Query("live", enum=[...])`
raised `TypeError` at IMPORT time and took the whole test suite down with it.
The fix spells the very same OpenAPI metadata as
`Query("live", json_schema_extra={"enum": [...]})`.

That fix is invisible to the eye: drop the `json_schema_extra` from one call
site and the app still imports, still serves, and the suite still passes -- the
published schema just quietly SHRINKS, and a client that reads the enum breaks
without anyone noticing. THIS test is the thing that notices.

It asserts the EXACT expected values, not merely that an `enum` key exists, so
a wrong list fails too. It also pins the nesting: the enum has to live inside
the parameter's own `schema` object, which is where a client reads it from.
"""

import pytest

from api.depthsight_api import app


# (path, query parameter name, expected enum list) for every migrated call site.
MIGRATED_ENUM_PARAMS = [
    (
        "/api/v1/admin/analytics/foundations",
        "source_type",
        ["backtest", "live", "paper"],
    ),
    (
        "/api/v1/admin/analytics/market-sentiment",
        "source_type",
        ["backtest", "live", "paper"],
    ),
    ("/api/v1/admin/logs/errors", "level", ["ERROR", "WARNING"]),
    ("/api/v1/leaderboard", "period", ["weekly", "monthly", "all_time"]),
    (
        "/api/v1/leaderboard",
        "category",
        ["sharpe_ratio", "net_pnl_percent"],
    ),
    ("/api/v1/market-data/candle-health", "mode", ["live", "paper"]),
    ("/api/v1/portfolio", "mode", ["live", "paper"]),
    ("/api/v1/positions", "mode", ["live", "paper"]),
    ("/api/v1/trades", "mode", ["live", "paper"]),
    ("/api/v1/strategies", "mode", ["live", "paper"]),
]


def _query_param_schema(path: str, name: str) -> dict:
    """The OpenAPI schema of one query parameter.

    Raises AssertionError if the path or the parameter is not published at all,
    so a renamed or deleted route fails loudly instead of silently skipping.
    """
    published = app.openapi()
    assert path in published["paths"], f"{path} is not in the published OpenAPI schema"

    operation = published["paths"][path]["get"]
    for param in operation.get("parameters", []):
        if param.get("name") == name and param.get("in") == "query":
            return param["schema"]

    raise AssertionError(f"query parameter {name!r} is not published on {path}")


@pytest.mark.parametrize("path, name, expected", MIGRATED_ENUM_PARAMS)
def test_migrated_query_param_still_publishes_its_enum(path, name, expected):
    """A migrated parameter must still advertise its enum in the OpenAPI schema.

    The `enum` is what a client generates its type from. Losing it does not
    break the server -- it breaks every consumer of the published schema, and
    nothing else in the suite would notice.
    """
    param_schema = _query_param_schema(path, name)

    assert "enum" in param_schema, (
        f"{path} ?{name}= no longer publishes an enum in its OpenAPI schema "
        f"(schema: {param_schema}). The json_schema_extra that replaced the "
        f"removed Query(enum=...) kwarg was dropped."
    )
    assert param_schema["enum"] == expected, (
        f"{path} ?{name}= publishes the wrong enum list: "
        f"{param_schema['enum']!r} != {expected!r}"
    )


def test_no_migrated_route_lost_an_enum():
    """Guard the guard: the migrated routes publish exactly what this file covers.

    If somebody migrates an eleventh `Query(enum=...)` and forgets to add it
    here, this test fails and tells them to. It is scoped to the migrated
    routes on purpose: other routes legitimately publish an enum because they
    use `typing.Literal` (e.g. /api/v1/portfolio/equity), and those are not
    this PR's business.
    """
    covered = {(path, name) for path, name, _expected in MIGRATED_ENUM_PARAMS}
    migrated_paths = {path for path, _name in covered}

    published = app.openapi()
    found = set()
    for path, item in published["paths"].items():
        if path not in migrated_paths:
            continue
        operation = item.get("get")
        if not isinstance(operation, dict):
            continue
        for param in operation.get("parameters", []) or []:
            if param.get("in") != "query":
                continue
            if "enum" in (param.get("schema") or {}):
                found.add((path, param["name"]))

    assert found == covered, (
        "the migrated routes do not publish exactly the enum parameters this "
        f"test covers.\n  published but not covered: {sorted(found - covered)}"
        f"\n  covered but not published: {sorted(covered - found)}"
    )
