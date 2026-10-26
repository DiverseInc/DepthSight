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

* Rule A - an `async for` over a session FACTORY CALL, or over a local name
  bound to one, whose loop variable is then HANDED TO an awaited call or has
  a session method called on it.

  A "factory call" is a call with no arguments, OR a call with arguments
  whose callee is one of this function's own parameters
  (`db_session_factory(...)`) or a method of this object
  (`self.get_db_session(...)`). The zero-argument case is what separates a
  session factory from `redis.scan_iter(match=..., count=100)` and
  `exchange.watch_order_book(symbol, None, params)`, both of which are
  loops over a call that is NOT a session.

* Rule B - an `await anext(gen)` (or `await gen.__anext__()`, or
  `await gen.asend(None)`), or an `await anext(<factory call>)`, where the
  generator is a local bound to a call and the generator is never CLOSED on
  the path that leaks. An inline `await anext(factory())` has no name to
  close it by, so it is leaked by construction.

Both rules look at how the value is USED, never at what the factory is
CALLED, so renaming `self.get_db_session` to `self._db_session` does not
hide a leak - and neither does binding it to another name first.

A close only counts when it is on the path that actually runs: a close
inside `if cond:`, inside a loop body, or inside an `except` handler may
never execute, and a close that may never execute closes nothing. A close
in a `finally:` always runs, and so does one in the body of the function.

SCOPE - the whole repo
----------------------
Every `*.py` file under the repository root is scanned: the repo ROOT itself
(`bot_runner.py`, `tasks.py`, `market_data_service.py`), `api/` (where the
factory is defined), `bot_module/`, `tools/`, `tests/` and the rest.

A guard that only scanned `bot_module/` would have been blind to the command
listener in `bot_runner.py` - one of the largest leaks this fix closed - and
blind to `api/`, where the session factory itself lives.

A file that does not parse is reported, not skipped: a guard that silently
ignores a file it cannot read is a guard that always reports OK.

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
import os
import sys
import warnings
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

REPO = Path(__file__).resolve().parent.parent

# Directories that never hold source we own. Everything else is scanned.
SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".mypy_cache",
        ".pytest_cache",
        ".pytest_tmp",
        ".ruff_cache",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        # Not Python source: front-end assets, runtime data, docs.
        "frontend",
        "pwa",
        "data",
        "logs",
        "docs",
    }
)

# Methods that only a DB session has. This is a SECONDARY signal - "the loop
# variable is being used as a session". The PRIMARY signal is name-free: the
# loop variable is handed to an AWAITED call. A leak whose body never calls
# a session method is still caught, because the awaited call alone is enough.
SESSION_METHODS = frozenset(
    {
        "add",
        "add_all",
        "begin",
        "bulk_save_objects",
        "commit",
        "delete",
        "execute",
        "expire",
        "expire_all",
        "flush",
        "get",
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


def _is_call(node: Optional[ast.AST]) -> bool:
    return isinstance(node, ast.Call)


def _same_expr(a: ast.AST, b: ast.AST) -> bool:
    """Structural equality for the two spellings of one generator."""
    if isinstance(a, ast.Name) and isinstance(b, ast.Name):
        return a.id == b.id
    if isinstance(a, ast.Attribute) and isinstance(b, ast.Attribute):
        return a.attr == b.attr and _same_expr(a.value, b.value)
    return False


def _own_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """The nodes that belong to THIS scope, and to no nested one.

    A nested `def`/`class` is its own scope, so its statements are NOT part
    of the enclosing function's body. Walking a whole module and treating
    every function's body as module-level code is how an `aclose()` in one
    function used to hide a leak in another: the module scope contains every
    function, so a close anywhere in the file masked an unclosed generator
    everywhere else in it.
    """
    body = getattr(scope, "body", None)
    if not isinstance(body, list):
        return
    stack: List[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _function_scopes(tree: ast.AST) -> List[ast.AST]:
    """Every function-ish scope: a module plus each def/async def."""
    scopes: List[ast.AST] = [tree]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scopes.append(node)
    return scopes


def _scope_parameters(scope: ast.AST) -> Set[str]:
    """The parameter names of a function scope."""
    if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return set()
    args = scope.args
    names = {
        a.arg
        for a in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
    }
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


def _target_holds(target: ast.AST, name: str) -> bool:
    if isinstance(target, ast.Name):
        return target.id == name
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_target_holds(element, name) for element in target.elts)
    return False


def _bound_value(
    name: str, scope: ast.AST, before: Optional[int] = None
) -> Optional[ast.AST]:
    """The value a local `name` was bound to, if any.

    `before` is the line of the use, so the binding that was in effect at
    that point wins. Only bindings made in THIS scope count. Plain
    assignment, annotated assignment and the walrus operator are all read,
    and a tuple target binds element-wise.
    """
    found: Optional[ast.AST] = None
    for node in _own_nodes(scope):
        lineno = getattr(node, "lineno", None)
        if before is not None and (lineno is None or lineno > before):
            continue
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, (ast.Tuple, ast.List)) and isinstance(
                    node.value, (ast.Tuple, ast.List)
                ):
                    for element, value in zip(target.elts, node.value.elts):
                        if _target_holds(element, name):
                            found = value
                elif _target_holds(target, name):
                    found = node.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                found = node.value
        elif isinstance(node, ast.NamedExpr):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                found = node.value
    return found


def _bound_attr_value(
    attr: str, scope: ast.AST, before: Optional[int] = None
) -> Optional[ast.AST]:
    """The value `self.<attr>` was bound to, if any."""
    found: Optional[ast.AST] = None
    for node in _own_nodes(scope):
        if not isinstance(node, ast.Assign):
            continue
        lineno = getattr(node, "lineno", None)
        if before is not None and (lineno is None or lineno > before):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Attribute)
                and target.attr == attr
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                found = node.value
    return found


def _resolved_bound_value(
    name: str, scope: ast.AST, before: Optional[int] = None, depth: int = 4
) -> Optional[ast.AST]:
    """The value a local `name` was bound to, following name-to-name aliases.

    `factory = self._db_session` then `factory()` is an alias of the factory;
    `gen = self.get_db_session()` then `alias = gen` is an alias of the
    generator. A guard that only follows the first hop is defeated by the
    second, so the chain is followed to a bounded depth.
    """
    value = _bound_value(name, scope, before)
    while depth > 0 and isinstance(value, ast.Name):
        depth -= 1
        value = _bound_value(value.id, scope, before)
    return value


def _is_factory_call(node: Optional[ast.AST], scope: ast.AST) -> bool:
    """Is this call a session factory?

    * a call with NO arguments - the shape every session factory in this
      repo has (`self.get_db_session()`, `db_session_factory()`). It is also
      what separates a factory from `redis.scan_iter(match=..., count=100)`
      and `exchange.watch_order_book(symbol, None, params)`, which are loops
      over a call that is not a session;
    * a call WITH arguments whose callee is one of this function's own
      parameters (`db_session_factory(user_id)`) or a method of this object
      (`self.get_db_session(user_id)`).
    """
    if not isinstance(node, ast.Call):
        return False
    if not node.args and not node.keywords:
        return True
    callee = node.func
    if isinstance(callee, ast.Name):
        return callee.id in _scope_parameters(scope)
    if (
        isinstance(callee, ast.Attribute)
        and isinstance(callee.value, ast.Name)
        and callee.value.id == "self"
    ):
        return True
    return False


def _awaited_calls(body: List[ast.stmt]) -> Set[int]:
    """The ids of the calls that are directly awaited."""
    awaited: Set[int] = set()
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            awaited.add(id(node.value))
    return awaited


def _loop_var_is_used_as_session(target: ast.AST, body: List[ast.stmt]) -> bool:
    """Is the loop variable handed to an awaited call, or used as a session?

    The PRIMARY signal is name-free: the loop variable is passed to an
    AWAITED call, as a positional argument or as a keyword's VALUE.

        await crud.get_config(db, user_id=...)      -> matched
        await crud.create_trade(db=db, ...)         -> matched

    The SECONDARY signal is a session method called on it (`db.commit()`,
    `db.get(...)`), which catches a body that only touches the session
    directly.
    """
    if not isinstance(target, ast.Name):
        return False
    name = target.id
    awaited = _awaited_calls(body)
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        # A session method called on the loop variable.
        if (
            isinstance(callee, ast.Attribute)
            and callee.attr in SESSION_METHODS
            and isinstance(callee.value, ast.Name)
            and callee.value.id == name
        ):
            return True
        # The loop variable handed to an awaited call.
        if id(node) not in awaited:
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            if isinstance(arg, ast.Name) and arg.id == name:
                return True
    return False


def _rule_a(tree: ast.AST, path: str) -> List[str]:
    """`async for` over a factory, where the loop var is used as a session."""
    findings: List[str] = []
    for scope in _function_scopes(tree):
        for node in _own_nodes(scope):
            if not isinstance(node, ast.AsyncFor):
                continue
            target = node.target
            if not isinstance(target, ast.Name):
                continue
            if not _loop_var_is_used_as_session(target, node.body):
                continue
            it = node.iter
            if _is_factory_call(it, scope):
                findings.append(f"{path}:{node.lineno}")
                continue
            if isinstance(it, ast.Name):
                bound = _resolved_bound_value(it.id, scope, before=node.lineno)
                if bound is not None and _is_factory_call(bound, scope):
                    findings.append(f"{path}:{node.lineno}")
    return findings


def _generator_is_closed(gen_expr: ast.AST, scope: ast.AST) -> bool:
    """Is the generator closed on the path that actually runs?

    A close inside `if cond:`, inside a loop body or inside an `except`
    handler may never execute, and a close that may never execute closes
    nothing. A close in a `finally:` always runs, and so does one in the
    body of the function.
    """
    found = False

    def visit(node: ast.AST, conditional: bool) -> None:
        nonlocal found
        if found:
            return
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            cond = conditional
            if isinstance(child, (ast.If, ast.For, ast.AsyncFor, ast.While)):
                cond = True
            elif isinstance(child, ast.Try):
                for stmt in child.body:
                    visit(stmt, cond)
                for handler in child.handlers:
                    for stmt in handler.body:
                        visit(stmt, True)
                for stmt in child.orelse:
                    visit(stmt, True)
                for stmt in child.finalbody:
                    visit(stmt, cond)
                continue
            elif isinstance(child, (ast.With, ast.AsyncWith)):
                for stmt in child.body:
                    visit(stmt, True)
                continue
            if isinstance(child, ast.Call):
                callee = child.func
                if (
                    isinstance(callee, ast.Attribute)
                    and callee.attr == "aclose"
                    and _same_expr(callee.value, gen_expr)
                    and not cond
                ):
                    found = True
                    return
            visit(child, cond)

    visit(scope, False)
    return found


def _rule_b(tree: ast.AST, path: str) -> List[str]:
    """`await anext(gen)` where the generator is never closed."""
    findings: List[str] = []
    for scope in _function_scopes(tree):
        for node in _own_nodes(scope):
            if not isinstance(node, ast.Await):
                continue
            call = node.value
            if not isinstance(call, ast.Call):
                continue
            callee = call.func
            gen_expr: Optional[ast.AST] = None
            if isinstance(callee, ast.Name) and callee.id in ("anext", "__anext__"):
                if call.args:
                    gen_expr = call.args[0]
            elif isinstance(callee, ast.Attribute) and callee.attr in (
                "__anext__",
                "asend",
            ):
                gen_expr = callee.value
            if gen_expr is None:
                continue
            if isinstance(gen_expr, ast.Call):
                # `await anext(factory())` - an inline generator. There is no
                # name to close it by, so it is abandoned by construction.
                findings.append(f"{path}:{node.lineno}")
                continue
            if isinstance(gen_expr, ast.Name):
                bound = _resolved_bound_value(gen_expr.id, scope, before=node.lineno)
            elif (
                isinstance(gen_expr, ast.Attribute)
                and isinstance(gen_expr.value, ast.Name)
                and gen_expr.value.id == "self"
            ):
                bound = _bound_attr_value(gen_expr.attr, scope, before=node.lineno)
            else:
                bound = None
            if not _is_call(bound):
                # Not a generator this rule can recognise (a parameter, an
                # object with `__anext__`). Not the shape this rule is about.
                continue
            if _generator_is_closed(gen_expr, scope):
                continue
            findings.append(f"{path}:{node.lineno}")
    return findings


def scanned_files(repo: Path = REPO) -> List[Path]:
    """Every Python file the guard reads.

    The WHOLE repo, not one package: the repo root holds the command
    listener, `api/` holds the session factory, and `bot_module/` holds the
    controller and the rehydrate path.
    """
    files: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIR_NAMES)
        for name in sorted(filenames):
            if name.endswith(".py"):
                files.append(Path(dirpath) / name)
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


def find_parse_failures(repo: Path = REPO) -> List[str]:
    """The files the guard could NOT read.

    A guard that silently skips a file it cannot parse is a guard that
    always reports OK, so these are reported loudly.
    """
    failures: List[str] = []
    for path in scanned_files(repo):
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            failures.append(f"{path.relative_to(repo).as_posix()}: unreadable")
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            try:
                ast.parse(source)
            except SyntaxError as exc:
                failures.append(f"{path.relative_to(repo).as_posix()}: {exc}")
    return failures


def _key(offender: str) -> Tuple[str, int]:
    path, _, line = offender.rpartition(":")
    return path, int(line)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    offenders = find_offenders()
    parse_failures = find_parse_failures()
    allowed = {_key(o) for o in offenders if _key(o) in ALLOWED}
    unexpected = [o for o in offenders if _key(o) not in ALLOWED]

    if args.json:
        print(
            json.dumps(
                {
                    "offenders": offenders,
                    "allowed": sorted(allowed),
                    "unexpected": unexpected,
                    "parse_failures": parse_failures,
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
        for failure in parse_failures:
            print(f"UNREADABLE  {failure}")
        print(
            f"\n{len(offenders)} flagged, {len(unexpected)} not allowed, "
            f"{len(parse_failures)} unreadable."
        )

    return 1 if unexpected or parse_failures else 0


if __name__ == "__main__":
    sys.exit(main())
