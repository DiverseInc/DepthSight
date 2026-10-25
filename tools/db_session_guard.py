#!/usr/bin/env python3
"""Guard: no code may acquire a DB session it does not own.

Why this exists
---------------
`api.database.get_db` is an async generator:

    async def get_db():
        async with AsyncSessionLocal() as session:
            yield session

Two ways of taking a session out of it BOTH leak the pooled connection when
the code moves on without closing the generator:

    async for db in get_db():        # the generator is abandoned
        ...                          # -> the transaction stays OPEN

    db_gen = get_db()                # the generator is abandoned
    db = await anext(db_gen)         # -> the transaction stays OPEN

The abandoned generator keeps its `async with AsyncSessionLocal()` suspended
at the `yield`, so the session keeps its pool connection checked out and its
transaction open until the garbage collector happens to finalise it. That
release is not something production can rely on: upstream measured ~120
pinned server connections at 660 controllers, which starved the pooler and
made every other query queue.

The correct shape is `bot_module.db_session.owned_session`:

    async with owned_session(self.get_db_session) as db:
        ...

which rolls the session back when the body raises and closes it either way.

HOW THE RULE WORKS - and why it is not a name list
--------------------------------------------------
An earlier version of this guard matched the factory by NAME
(`get_db_session`, `db_session_factory`). That is weak twice over: a rename
or an alias defeats it, and it could not see the `anext` acquisition form at
all - so it would never have flagged the very bug it was written for.

This version matches the SHAPE of the acquisition instead:

* Rule A - an `async for` over a ZERO-ARGUMENT CALL (a factory), or over a
  local name bound to one, whose loop variable is then HANDED TO another
  call or has a session method called on it. A zero-argument call is what
  distinguishes a session factory from `exchange.watch_order_book(...)` or
  `redis.scan_iter(...)`, both of which take arguments. A rename of the
  factory does not change any of this.

* Rule B - an `await anext(gen)` (or `await gen.__anext__()`) where `gen`
  is a local bound to a call, and `gen.aclose()` is never called in the
  same function. That is the acquisition with no release.

Both rules look at how the value is USED, never at what the factory is
CALLED, so renaming `self.get_db_session` to `self._db_session` does not
hide a leak.

Usage
-----
    python tools/db_session_guard.py            # human-readable report
    python tools/db_session_guard.py --json     # machine-readable

Exit code 0 = clean, 1 = offenders outside ALLOWED.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import warnings
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

REPO = Path(__file__).resolve().parent.parent

# Where session-handling code lives. The repo ROOT matters: `bot_runner.py`
# holds the command listener, whose session is acquired in the `anext` form.
SCAN_DIRS = ("api", "bot_module", "scripts", "tests")
SCAN_ROOT_FILES = ("bot_runner.py", "tasks.py")

# Methods that only a DB session has. A loop variable with one of these
# called on it is being used as a session.
SESSION_METHODS = frozenset(
    {
        "add",
        "add_all",
        "begin",
        "commit",
        "delete",
        "execute",
        "expire",
        "flush",
        "merge",
        "refresh",
        "rollback",
        "scalar",
        "scalars",
    }
)

# Known leftovers. Each entry is a file:line the rules MATCH but that cannot
# be fixed without a bigger refactor. This is deliberately NOT a way to make
# the guard green: `tests/test_db_session_guard.py` asserts the scanner
# finds EXACTLY this set, so a new offender can never be added here
# quietly, and an entry that stops matching (because it was fixed) fails
# the test until it is removed.
ALLOWED: Dict[Tuple[str, int], str] = {
    ("bot_runner.py", 397): (
        "The shard's bootstrap session is also the write session of every "
        "controller (bot_module/paper_executor.py:81), so it cannot be "
        "released here without the per-controller session refactor "
        "(upstream's `_controller_session()` / `session_scope`). It pins ONE "
        "connection per worker for the whole run. This site is NOT covered "
        "by this guard."
    ),
}


def _dotted(node: ast.AST) -> Optional[str]:
    """`crud.get_config` -> "crud.get_config"; `db.commit` -> "db.commit"."""
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    else:
        return None
    return ".".join(reversed(parts))


def _is_zero_arg_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and not node.args and not node.keywords


def _bound_call(
    name: str, scope: ast.AST, before: Optional[int] = None
) -> Optional[ast.Call]:
    """The call a local `name` was bound to, if any.

    `before` is the line of the use, so the binding that was in effect at
    that point wins.
    """
    found: Optional[ast.Call] = None
    for node in ast.walk(scope):
        if not isinstance(node, ast.Assign):
            continue
        if before is not None and node.lineno > before:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                if _is_zero_arg_call(node.value):
                    found = node.value
                else:
                    found = None
    return found


def _function_scopes(tree: ast.AST) -> List[ast.AST]:
    """Every function-ish scope: a module plus each def/async def."""
    scopes: List[ast.AST] = [tree]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scopes.append(node)
    return scopes


def _loop_var_is_used_as_session(target: ast.AST, body: List[ast.stmt]) -> bool:
    """Is the loop variable handed to a call, or used as a session?"""
    if not isinstance(target, ast.Name):
        return False
    name = target.id
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        # A session method called on the loop variable.
        if isinstance(callee, ast.Attribute) and callee.attr in SESSION_METHODS:
            if isinstance(callee.value, ast.Name) and callee.value.id == name:
                return True
        # The loop variable handed to another call.
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            if isinstance(arg, ast.Name) and arg.id == name:
                return True
    return False


def _rule_a(tree: ast.AST, path: str) -> List[str]:
    """`async for` over a factory, where the loop var is used as a session."""
    findings: List[str] = []
    for scope in _function_scopes(tree):
        for node in ast.walk(scope):
            if not isinstance(node, ast.AsyncFor):
                continue
            target = node.target
            if not isinstance(target, ast.Name):
                continue
            if not _loop_var_is_used_as_session(target, node.body):
                continue
            it = node.iter
            if _is_zero_arg_call(it):
                findings.append(f"{path}:{node.lineno}")
                continue
            if isinstance(it, ast.Name):
                bound = _bound_call(it.id, scope, before=node.lineno)
                if bound is not None:
                    findings.append(f"{path}:{node.lineno}")
    return findings


def _generator_is_closed(name: str, scope: ast.AST) -> bool:
    for node in ast.walk(scope):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if (
            isinstance(callee, ast.Attribute)
            and callee.attr == "aclose"
            and isinstance(callee.value, ast.Name)
            and callee.value.id == name
        ):
            return True
    return False


def _rule_b(tree: ast.AST, path: str) -> List[str]:
    """`await anext(gen)` where the generator is never closed."""
    findings: List[str] = []
    for scope in _function_scopes(tree):
        for node in ast.walk(scope):
            if not isinstance(node, ast.Await):
                continue
            call = node.value
            if not isinstance(call, ast.Call):
                continue
            callee = call.func
            gen_name: Optional[str] = None
            if isinstance(callee, ast.Name) and callee.id in ("anext", "__anext__"):
                if call.args and isinstance(call.args[0], ast.Name):
                    gen_name = call.args[0].id
            elif (
                isinstance(callee, ast.Attribute)
                and callee.attr == "__anext__"
                and isinstance(callee.value, ast.Name)
            ):
                gen_name = callee.value.id
            if gen_name is None:
                continue
            if _generator_is_closed(gen_name, scope):
                continue
            findings.append(f"{path}:{node.lineno}")
    return findings


def scanned_files(repo: Path = REPO) -> List[Path]:
    files: List[Path] = []
    for name in SCAN_ROOT_FILES:
        candidate = repo / name
        if candidate.is_file():
            files.append(candidate)
    for directory in SCAN_DIRS:
        base = repo / directory
        if base.is_dir():
            files.extend(sorted(base.rglob("*.py")))
    return files


def find_offenders_in_source(source: str, path: str = "<source>") -> List[str]:
    """The offenders in one source string. Used by the tests."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []
    return sorted(set(_rule_a(tree, path) + _rule_b(tree, path)))


def find_offenders(repo: Path = REPO) -> List[str]:
    """Every acquisition the rules flag, as "path:line" strings."""
    offenders: List[str] = []
    for path in scanned_files(repo):
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        offenders.extend(find_offenders_in_source(source, path.relative_to(repo).as_posix()))
    return sorted(set(offenders))


def _key(offender: str) -> Tuple[str, int]:
    path, _, line = offender.rpartition(":")
    return path, int(line)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    offenders = find_offenders()
    allowed = {_key(o) for o in offenders if _key(o) in ALLOWED}
    unexpected = [o for o in offenders if _key(o) not in ALLOWED]

    if args.json:
        print(
            json.dumps(
                {
                    "offenders": offenders,
                    "allowed": sorted(allowed),
                    "unexpected": unexpected,
                },
                indent=2,
            )
        )
    else:
        for offender in offenders:
            key = _key(offender)
            if key in ALLOWED:
                print(f"ALLOWED  {offender}  {ALLOWED[key]}")
            else:
                print(f"LEAK     {offender}")
        print(f"\n{len(offenders)} flagged, {len(unexpected)} not allowed.")

    return 1 if unexpected else 0


if __name__ == "__main__":
    sys.exit(main())
