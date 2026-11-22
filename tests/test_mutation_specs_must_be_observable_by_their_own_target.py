# tests/test_mutation_specs_must_be_observable_by_their_own_target.py
"""A mutation spec may only carry mutants its OWN pytest_target can see.

THE DEFECT THIS PINS
--------------------
On 2026-11-09 the mutation harness ran
`tools/mutations/source_offer_deployed_commit.json` and reported ONE survivor:

    SURVIVED (tests do NOT defend this -- real gap): M4: the deploy stamp
    stops recording the commit it is about to build

M4 mutates `ops/scripts/record_deployed_commit.sh`. The product was NOT
undefended: the stamp script's own tests are in
`tests/test_record_deployed_commit_script.py` and
`tests/test_deployed_commit_public_ref.py`, and they kill the mutant. But
those two files are in the target of the OTHER spec
(`tools/mutations/deployed_commit_script.json`), NOT in
`source_offer_deployed_commit.json`'s target. So M4 was filed under a spec
whose tests could not observe it, and the harness reported a survivor for a
FILING reason -- which reads, to a human, like a hole in the product.

That is a SILENT GAP, and it is the class of defect this file exists to stop:
a mutant filed under a spec whose target cannot see it. The fix for M4 was to
file it where it can be seen; this file is what stops it recurring.

WHY "MENTIONS THE FILE" IS NOT THE RULE (MEASURED, NOT ASSUMED)
----------------------------------------------------------------
The obvious rule -- "the spec's target must contain a test file whose source
mentions the mutated file" -- is VACUOUS for this defect, and it was measured
to be so. Running the M4 mutation against each candidate test file
individually gives:

    CATCHES M4 (rc != 0):
        tests/test_record_deployed_commit_script.py    10 failing tests
        tests/test_deployed_commit_public_ref.py       16 failing tests
        tests/test_deployed_commit_mutation_needles.py 1 failing test
    DOES NOT SEE IT (rc == 0):
        tests/test_update_sh_records_the_deployed_commit.py
        tests/test_update_sh_never_writes_the_stamp.py
        tests/test_public_source_offer.py
        tests/test_the_promised_source_archive_is_fetched.py

`tests/test_update_sh_records_the_deployed_commit.py` MENTIONS
`ops/scripts/record_deployed_commit.sh` (it asserts the script exists, and
that `update.sh` and `AGENTS.md` call it) -- and it is COMPLETELY BLIND to
M4. A rule built on mentions would therefore have been GREEN on the exact
case it was written for. A guard that cannot fail is worse than no guard.

THE RULE ACTUALLY ENFORCED
--------------------------
A test OBSERVES a file F when the CONTENT of F reaches the test. For a
NON-PYTHON file (a shell script, a config, a dotfile, a frontend source) that
means: the test READS it, or EXECUTES it, addressed BY PATH.

Two properties are therefore required of every spec in `tools/mutations/*.json`:

  R1 (COVERAGE)   For every NON-PYTHON file F the spec mutates, the spec's own
                  `pytest_target` contains at least one test that observes F.
                  Without such a test, EVERY mutant of F is guaranteed to
                  survive, whatever the product is. THIS IS THE INVARIANT.
                  It is NEVER excused by the allowlist.

  R2 (COMPLETENESS)
                  For every NON-PYTHON file F the spec mutates, EVERY test in
                  the repository that observes F is in the spec's
                  `pytest_target`, unless the spec is on the explicit
                  allowlist below. R2 is the allowlistable half: it can be
                  unsatisfiable when two specs deliberately guard DIFFERENT
                  defects in the SAME file.

R2 is the stronger half, and it is the half with the power to reject M4's
original filing: the two tests that actually catch M4 read the stamp script's
bytes, and neither was in `source_offer_deployed_commit.json`'s target.

WHY PYTHON MODULES ARE NOT IN THE STATIC RULE
----------------------------------------------
For a Python module, "observes" means something BEHAVIOURAL: the module is
imported, or reached through the FastAPI app, or its source is analysed as
part of a whole package. The observer set of `bot_module/controller.py` is 58
test files; of `api/schemas.py` it is 31. A static per-file proxy for that
was measured to produce FALSE POSITIVES on pre-existing specs that the
harness itself shows to be sound (the target reaches the module through the
app fixture, or through a package-wide source walk such as
`tests/test_controller_db_session_leak.py`, which parses every `.py` under
`bot_module/`). A rule that rejects sound specs is a rule that gets ignored.

So the static rule covers NON-PYTHON files, where the proxy is EXACT -- it
reproduces the measured ground truth above for the stamp script, and it
correctly finds the dotfile readers that reach their subject through a
helper function. For module mutants, the harness remains the authority: it
runs the spec's own target and reports a survivor if the target cannot kill
the mutant. A module mutant that survives that way is a REAL finding, and
this file does not paper over it.

OBSERVABILITY IS MEASURED, NOT GUESSED
--------------------------------------
"Reads or executes F's content" is decided by STATIC ANALYSIS of the test
files. The analysis resolves, inside a test file:

  * module-level constants (`DOCKERIGNORE = REPO_ROOT / ".dockerignore"`),
  * local aliases inside a function (`script = REPO_ROOT / "update.sh"`),
  * helper functions whose parameter carries the path
    (`read_patterns(DOCKERIGNORE)` -> `path.read_text(...)`).

A path that is only known at RUNTIME -- a file yielded by `os.walk`, a
`glob` result -- is NOT attributed to a specific file. That is deliberate:
`tests/test_update_sh_never_writes_the_stamp.py` walks every `.sh` in the
repository and reads them all, but it addresses ONE of them by path
(`update.sh`), so the walk is not attributed to every shell script in the
repository -- counting it as an observer of all of them would make R2
meaningless.

A file's RULES can also reach a test without the test reading its bytes: a
test that runs `git check-ignore` observes `.gitignore`, and a test that runs
`docker build` observes `.dockerignore`. Those are counted too, or the rule
would reject a sound spec.

The needle-audit file `tests/test_deployed_commit_mutation_needles.py` reads
whatever file its spec names, so it observes every mutated file by
construction. It is EXEMPT from R2 on purpose: it must not be in any
`pytest_target` (it reads the mutated file, so running it DURING a mutation
would fail for the wrong reason), and `tests/test_deployed_commit_mutation_
needles.py` already enforces exactly that. This file holds itself to the same
rule.

THE ALLOWLIST IS A REVIEW, NOT A CONVENIENCE
---------------------------------------------
Every entry names the spec, the reason it cannot satisfy R2, and the date the
entry was last reviewed. A spec gets in here because R2 cannot be satisfied
for it as written -- NOT because "it probably works". A spec that starts
satisfying R2 has its entry REMOVED (there is a test for that). The allowlist
never excuses R1: the invariant holds for every spec, always.
"""

import ast
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MUTATIONS_DIR = REPO_ROOT / "tools" / "mutations"

THIS_FILE = "tests/test_mutation_specs_must_be_observable_by_their_own_target.py"

# The needle-audit file reads whatever file its spec names, so it observes
# every mutated file by construction. It must never be in a pytest_target
# (it reads the mutated file, so during a mutation it would fail for the
# wrong reason), so it is exempt from R2.
NEEDLE_AUDIT = "tests/test_deployed_commit_mutation_needles.py"

# This file reads the specs and the test sources, so it observes every
# mutated TEST FILE by construction. Same exemption, same reason.
R2_EXEMPT = frozenset({NEEDLE_AUDIT, THIS_FILE})

# Calls that READ a file's content or EXECUTE it.
READERS = {
    "read_text", "read_bytes", "readlines", "open",
    "run", "check_output", "check_call", "Popen", "call", "system",
}

# The subset that RUNS another program (and can therefore consult a file's
# rules without reading its bytes).
RUNNERS = {"run", "check_output", "check_call", "Popen", "call", "system"}


def _string_literals(node):
    """Every string constant inside a call's arguments."""
    found = []
    for argument in list(node.args) + [kw.value for kw in node.keywords]:
        for literal in ast.walk(argument):
            if isinstance(literal, ast.Constant) and isinstance(literal.value, str):
                found.append(literal.value)
    return found

# How deep a helper call may be resolved before the analysis gives up.
MAX_HELPER_DEPTH = 3

# A test can observe a file's RULES without ever reading its bytes: it runs a
# tool that consults the file. `git check-ignore` consults `.gitignore`;
# `docker build` consults `.dockerignore`. These are the measured cases in
# this repository -- `tests/test_gitignore_ignores_env_copies.py` asks git
# whether a path is ignored, and git answers from `.gitignore`'s rules.
# Each entry is (tool literals, subcommand literals, the file observed).
TOOL_OBSERVED_FILES = (
    (("git",), ("check-ignore", "ls-files", "status", "check-attr"), ".gitignore"),
    (("docker",), ("build", "buildx"), ".dockerignore"),
)


# --------------------------------------------------------------------------
# The allowlist: specs that cannot satisfy R2 (completeness) as written.
#
# R1 (coverage) is NEVER excused here -- it is the invariant. Every entry
# below is a case where two specs deliberately guard DIFFERENT defects in the
# SAME file, so each spec's target holds only the tests that guard its own
# defect.
#
# Format: (spec name, reason, last-reviewed date)
# --------------------------------------------------------------------------
ALLOWLIST = (
    (
        "deployed_commit_script.json",
        "R2 cannot hold across SPECS: `ops/scripts/record_deployed_commit.sh` "
        "is also read by `tests/test_update_sh_never_writes_the_stamp.py`, "
        "which guards a DIFFERENT defect in the same script (that only the "
        "stamp script may assign DEPLOYED_COMMIT). This spec's own target "
        "already holds both tests that kill its mutants. R1 (coverage) holds.",
        "2026-11-09",
    ),
    (
        "dockerignore_tracked_files.json",
        "R2 cannot hold across SPECS: `.dockerignore` is also read by "
        "`tests/test_dockerignore_covers_gitignore.py` (rule parity with "
        "`.gitignore`), by `tests/test_dockerignore_excludes_secrets.py` and "
        "by `tests/test_production_bundle_excludes_demo_credentials.py`. "
        "Those tests guard CREDENTIALS and RULE PARITY; this spec guards the "
        "BUILD CONTEXT (a pattern must not sweep a tracked file out of it). "
        "R1 (coverage) holds: this spec's own target reads the dotfile.",
        "2026-11-09",
    ),
    (
        "gitignore_env_copies.json",
        "R2 cannot hold across SPECS: `.gitignore` is also read by "
        "`tests/test_dockerignore_covers_gitignore.py`, which guards the "
        "rule PARITY between the two dotfiles; this spec guards the "
        "ENVIRONMENT COPY class. R1 (coverage) holds: this spec's own target "
        "observes `.gitignore` by asking git (`git check-ignore`), which "
        "consults the file's rules.",
        "2026-11-09",
    ),
    (
        "source_offer_deployed_commit.json",
        "R2 cannot hold across SPECS: `update.sh` and `ops/scripts/smoke-test.sh` "
        "are also read by `tests/test_the_offer_is_served_over_a_verified_certificate.py`, "
        "which guards the CERTIFICATE; this spec guards the DEPLOYED COMMIT "
        "STAMP. R1 (coverage) holds: this spec's own target observes every "
        "file it mutates.",
        "2026-11-09",
    ),
    (
        "source_offer_image_and_certificate.json",
        "R2 cannot hold across SPECS: `.dockerignore`, `update.sh` and "
        "`ops/scripts/smoke-test.sh` are also read by the dockerignore spec's "
        "and the deployed-commit spec's own tests. Those specs guard the "
        "BUILD CONTEXT and the COMMIT STAMP; this one guards the IMAGE and "
        "the CERTIFICATE. R1 (coverage) holds.",
        "2026-11-09",
    ),
    (
        "source_offer_pinned_rewrite.json",
        "R2 cannot hold across SPECS: `caddy.config` is also read by "
        "`tests/test_public_source_offer.py`, which guards the PUBLIC OFFER; "
        "this spec guards the PINNED REWRITE. R1 (coverage) holds.",
        "2026-11-09",
    ),
    (
        "build_context_dockerignore.json",
        "R2 cannot hold across SPECS: `frontend/.dockerignore` is also read "
        "by `tests/test_production_bundle_excludes_demo_credentials.py`, "
        "which guards the DEMO CREDENTIAL class in the web build contexts; "
        "this spec guards the BUILD CONTEXT (a context that copies the whole "
        "context needs its own rule set, and it must not sweep a tracked "
        "file out of it). `pwa/.dockerignore` is observed by this spec's own "
        "target alone. R1 (coverage) holds: this spec's own target reads both "
        "dotfiles.",
        "2026-11-11",
    ),
)

# Bump this when the allowlist changes: a change to it is a REVIEW, not a
# side effect of editing the rule.
ALLOWLIST_LAST_REVIEWED = "2026-11-11"

ALLOWLIST_NAMES = tuple(name for name, _reason, _date in ALLOWLIST)


# --------------------------------------------------------------------------
# Static analysis: what does one test file observe?
# --------------------------------------------------------------------------
def _parse(path: Path):
    try:
        return ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return None


def _module_parts(rel_path: str):
    """The dotted module name of a repo-relative Python file."""
    if rel_path.endswith(".py"):
        parts = rel_path[:-3].split("/")
    else:
        parts = rel_path.split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return parts


def _scope_nodes(body):
    """Every node that belongs to THIS scope.

    A nested function, method or class has its own scope, so its statements
    are NOT part of this one. Without this, an assignment in a method would
    overwrite a same-named assignment in another method, and the analysis
    would attribute a read to the wrong file.
    """
    nodes = []

    def visit(statements):
        for node in statements:
            nodes.append(node)
            for child in ast.iter_child_nodes(node):
                if isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    continue
                visit([child])

    visit(list(body))
    return nodes


class _PathScope:
    """Resolves path-ish expressions in one parsed Python file.

    An environment maps a NAME to either an unevaluated expression or an
    already-resolved list of path parts. Resolution is deliberately
    conservative: a path that is only known at runtime (an `os.walk` result,
    a `glob` result) resolves to nothing, so it is never attributed to a
    specific file.
    """

    def __init__(self, tree):
        self.tree = tree
        self.functions = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions.setdefault(node.name, node)

    # -- environment ------------------------------------------------------
    def module_env(self):
        env = {}
        for node in self.tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        env[target.id] = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                env[node.target.id] = node.value
        return env

    def function_env(self, func, module_env):
        """The module environment plus the function's own local assignments."""
        env = dict(module_env)
        params = func.args
        for arg in list(params.posonlyargs) + list(params.args) + list(params.kwonlyargs):
            env[arg.arg] = None
        for node in _scope_nodes(func.body):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        env.setdefault(target.id, node.value)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                env.setdefault(node.target.id, node.value)
        return env

    # -- path parts -------------------------------------------------------
    def parts(self, node, env, depth=0):
        """The path-ish string parts of an expression, in order."""
        if node is None or depth > MAX_HELPER_DEPTH:
            return []
        if isinstance(node, ast.Constant):
            return [node.value] if isinstance(node.value, str) else []
        if isinstance(node, ast.Name):
            value = env.get(node.id)
            if value is None:
                return []
            if isinstance(value, list):
                return list(value)
            return self.parts(value, env, depth)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return self.parts(node.left, env, depth) + self.parts(node.right, env, depth)
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name == "Path":
                out = []
                for arg in list(node.args) + [kw.value for kw in node.keywords]:
                    out += self.parts(arg, env, depth)
                return out
            return []
        if isinstance(node, (ast.List, ast.Tuple)):
            out = []
            for element in node.elts:
                out += self.parts(element, env, depth)
            return out
        return []


def _joined(parts):
    """Join path parts into one path string."""
    return "/".join(part for part in parts if part)


def _names_file(candidate, target):
    """Does this path denote `target` (repo-relative, POSIX)?"""
    if not candidate:
        return False
    normalised = candidate.replace("\\", "/")
    while normalised.startswith("./"):
        normalised = normalised[2:]
    return normalised == target or normalised.endswith("/" + target)


def _collect_reads(tree):
    """Every path the file READS or EXECUTES, as path strings."""
    scope = _PathScope(tree)
    module_env = scope.module_env()
    scopes = {
        name: scope.function_env(func, module_env)
        for name, func in scope.functions.items()
    }
    reads = set()
    expanded = set()

    def scan(body, env, depth):
        if depth > MAX_HELPER_DEPTH:
            return
        nodes = _scope_nodes(body)
        local = dict(env)
        for node in nodes:
            if isinstance(node, ast.Assign):
                for target_node in node.targets:
                    if isinstance(target_node, ast.Name):
                        local[target_node.id] = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                local[node.target.id] = node.value
        for node in nodes:
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name in READERS:
                candidates = []
                receiver = func.value if isinstance(func, ast.Attribute) else None
                if receiver is not None:
                    candidates.append(_joined(scope.parts(receiver, local, depth)))
                for arg in list(node.args) + [kw.value for kw in node.keywords]:
                    candidates.append(_joined(scope.parts(arg, local, depth)))
                    if isinstance(arg, (ast.List, ast.Tuple)):
                        for element in arg.elts:
                            candidates.append(
                                _joined(scope.parts(element, local, depth))
                            )
                for candidate in candidates:
                    if candidate:
                        reads.add(candidate)
                # A tool that consults a file's RULES observes that file,
                # even though the test never reads its bytes.
                if name in RUNNERS:
                    literals = {
                        literal.lower()
                        for literal in _string_literals(node)
                        if literal
                    }
                    for tool, subcommands, observed in TOOL_OBSERVED_FILES:
                        if (literals & set(tool)) and (literals & set(subcommands)):
                            reads.add(observed)
            elif name in scope.functions:
                helper = scope.functions[name]
                bound = dict(scopes.get(name, module_env))
                params = helper.args
                positional = list(params.posonlyargs) + list(params.args)
                for index, arg in enumerate(node.args):
                    if index < len(positional):
                        bound[positional[index].arg] = scope.parts(arg, local, depth + 1)
                for kw in node.keywords:
                    if kw.arg:
                        bound[kw.arg] = scope.parts(kw.value, local, depth + 1)
                signature = (
                    name,
                    tuple(
                        sorted(
                            (key, tuple(value))
                            for key, value in bound.items()
                            if isinstance(value, list)
                        )
                    ),
                )
                if signature in expanded:
                    continue
                expanded.add(signature)
                scan(helper.body, bound, depth + 1)

    scan(tree.body, module_env, 0)
    for name, func in scope.functions.items():
        scan(func.body, scopes.get(name, module_env), 0)
    return reads


def _collect_imports(tree, rel_path):
    """Every module name the file imports."""
    importer_module = ".".join(_module_parts(rel_path))
    package = importer_module.rsplit(".", 1)[0] if "." in importer_module else ""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".") if package else []
                if node.level > 1:
                    base = base[: len(base) - (node.level - 1)]
                base_module = ".".join(
                    base + (node.module.split(".") if node.module else [])
                )
            else:
                base_module = node.module or ""
            if base_module:
                found.add(base_module)
            for alias in node.names:
                found.add(f"{base_module}.{alias.name}" if base_module else alias.name)
    return found


_ANALYSIS_CACHE = {}


def analyse(test_path: Path):
    """(paths read, modules imported) by one test file. Cached."""
    key = str(test_path)
    if key not in _ANALYSIS_CACHE:
        tree = _parse(test_path)
        if tree is None:
            _ANALYSIS_CACHE[key] = (frozenset(), frozenset())
        else:
            try:
                rel = test_path.relative_to(REPO_ROOT).as_posix()
            except ValueError:
                rel = test_path.name
            _ANALYSIS_CACHE[key] = (
                frozenset(_collect_reads(tree)),
                frozenset(_collect_imports(tree, rel)),
            )
    return _ANALYSIS_CACHE[key]


def reads_file(test_path: Path, target: str) -> bool:
    """Does this test READ or EXECUTE `target`'s CONTENT, by path?

    Deliberately NOT "mentions": a test that only names the file cannot
    observe a defect injected into it. That distinction is the whole point
    of this file -- see the module docstring for the measurement.
    """
    reads, _modules = analyse(test_path)
    return any(_names_file(candidate, target) for candidate in reads)


def imports_module(test_path: Path, target: str) -> bool:
    """Does this test IMPORT the Python module `target`?

    Importing a module EXECUTES it, so a defect inside the module reaches the
    test. That is what makes a module observable in a way that merely naming
    it is not.
    """
    _reads, modules = analyse(test_path)
    return ".".join(_module_parts(target)) in modules


def observes(test_rel: str, target_rel: str) -> bool:
    """Can this test SEE a change in the CONTENT of `target_rel`?

    For a Python module, importing it counts as well: importing executes it.
    """
    test_path = REPO_ROOT / test_rel
    if target_rel.endswith(".py"):
        return imports_module(test_path, target_rel) or reads_file(test_path, target_rel)
    return reads_file(test_path, target_rel)


# --------------------------------------------------------------------------
# Repository helpers
# --------------------------------------------------------------------------
def repository_test_files():
    """Every test file in the repository, repo-relative and POSIX."""
    found = set()
    for path in REPO_ROOT.rglob("*.py"):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if path.name.startswith("test_") or path.name.endswith("_test.py"):
            found.add(relative)
        elif "/tests/" in "/" + relative:
            found.add(relative)
    return sorted(found)


def spec_files():
    """Every mutation spec, by name."""
    return sorted(path.name for path in MUTATIONS_DIR.glob("*.json"))


def load_spec(name):
    path = MUTATIONS_DIR / name
    assert path.is_file(), f"the mutation spec must exist: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def mutated_files(spec):
    """The distinct files a spec mutates, in order."""
    seen = []
    for mutation in spec.get("mutations", []):
        if mutation["file"] not in seen:
            seen.append(mutation["file"])
    return seen


def target_of(spec):
    """The spec's pytest_target, as a list of repo-relative paths."""
    return spec.get("pytest_target", "").split()


def is_static_scope(mutated):
    """Is the static rule applied to this file?

    Only NON-PYTHON files are in the static rule's scope: for a module,
    observation is behavioural (an import, an app fixture, a package-wide
    source walk) and a static per-file proxy was measured to reject sound
    specs. See the module docstring.
    """
    return not mutated.endswith(".py")


# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------
def coverage_problems(name, spec, observers_of):
    """R1 (coverage): the spec's own target must observe what it mutates.

    THIS IS THE INVARIANT. It is never excused by the allowlist: a mutant
    filed under a spec whose own pytest_target cannot see it is a SILENT GAP.
    """
    problems = []
    targets = target_of(spec)
    for mutated in mutated_files(spec):
        if not (REPO_ROOT / mutated).is_file():
            problems.append(
                f"{name}: mutation names a file that does not exist: {mutated}"
            )
            continue
        if not is_static_scope(mutated):
            continue
        if not (observers_of(mutated) & set(targets)):
            problems.append(
                f"{name}: no test in pytest_target observes {mutated}, so every "
                f"mutant of it would survive: target={targets}"
            )
    return problems


def completeness_problems(name, spec, observers_of):
    """R2 (completeness): every observer of a mutated file is in the target.

    R2 is the allowlistable half: it can be unsatisfiable when two specs
    deliberately guard DIFFERENT defects in the same file.
    """
    problems = []
    targets = set(target_of(spec))
    for mutated in mutated_files(spec):
        if not (REPO_ROOT / mutated).is_file():
            continue
        if not is_static_scope(mutated):
            continue
        outside = observers_of(mutated) - targets - R2_EXEMPT
        if outside:
            problems.append(
                f"{name}: tests that observe {mutated} are missing from "
                f"pytest_target: {sorted(outside)}"
            )
    return problems


def problems_for(name, spec, observers_of):
    """The problems the rule raises for `spec`, with the allowlist applied.

    The allowlist excuses R2 (completeness) only. R1 (coverage) is never
    excused.
    """
    problems = coverage_problems(name, spec, observers_of)
    if name not in ALLOWLIST_NAMES:
        problems += completeness_problems(name, spec, observers_of)
    return problems


# --------------------------------------------------------------------------
# The rule, applied to the repository
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def test_files():
    return repository_test_files()


@pytest.fixture(scope="module")
def observers_cache(test_files):
    """mutated file -> set of tests that observe its content."""
    cache = {}

    def observers_of(mutated):
        if mutated not in cache:
            cache[mutated] = {
                test for test in test_files if observes(test, mutated)
            }
        return cache[mutated]

    return observers_of


@pytest.mark.parametrize("spec_name", spec_files())
def test_the_spec_can_see_the_files_it_mutates(spec_name, test_files, observers_cache):
    """Every spec must be able to observe every file it mutates.

    R1 (coverage) is enforced for EVERY spec and is NEVER excused by the
    allowlist -- it is the invariant this file exists to hold. R2
    (completeness) is enforced for every spec except the ones on the
    documented allowlist.
    """
    spec = load_spec(spec_name)
    problems = problems_for(spec_name, spec, observers_cache)

    assert not problems, (
        "the mutation spec cannot observe what it mutates:\n  "
        + "\n  ".join(problems)
        + "\nA mutant filed under a spec whose own pytest_target cannot see "
        "it is a SILENT GAP: the harness reports a survivor, which reads as "
        "a hole in the PRODUCT when the truth is that the MUTANT is filed "
        "in the wrong place."
    )


# --------------------------------------------------------------------------
# The rule, applied to the MEASURED defect
# --------------------------------------------------------------------------
# The M4 mutant exactly as it was filed, byte-identical to the original.
M4_MUTANT = {
    "label": "M4: the deploy stamp stops recording the commit it is about to build",
    "file": "ops/scripts/record_deployed_commit.sh",
    "needle": "DEPLOYED_COMMIT=$(git rev-parse HEAD)",
    "replace": "DEPLOYED_COMMIT=\"\"",
}

# The tests that CATCH M4, as measured by running the mutation against each
# candidate test file individually (see the module docstring).
THE_THREE_DEFENDERS = (
    "tests/test_record_deployed_commit_script.py",
    "tests/test_deployed_commit_public_ref.py",
    "tests/test_deployed_commit_mutation_needles.py",
)


def _spec_without(spec_name, drop_from_target=()):
    """The on-disk spec, with the named files removed from its target."""
    spec = json.loads(json.dumps(load_spec(spec_name)))
    spec["pytest_target"] = " ".join(
        target
        for target in target_of(spec)
        if target not in drop_from_target
    )
    return spec


def test_the_original_m4_filing_is_rejected_by_this_rule(test_files, observers_cache):
    """THE NEGATIVE CONTROL, on the REAL defect.

    This reconstructs M4's original filing -- M4 back in
    `source_offer_deployed_commit.json`, with the tests that catch it absent
    from that spec's pytest_target -- and asserts the rule REJECTS it. If
    this test passed on that shape, the rule would be a guard that cannot
    fail, and every other assertion in this file would be worthless.
    """
    spec = _spec_without("source_offer_deployed_commit.json")
    spec["mutations"].append(M4_MUTANT)
    spec["pytest_target"] = " ".join(
        target
        for target in target_of(spec)
        if target not in THE_THREE_DEFENDERS
    )

    problems = coverage_problems(
        "source_offer_deployed_commit.json (M4's ORIGINAL filing)",
        spec,
        observers_cache,
    )
    assert problems, (
        "the rule ACCEPTED M4's original filing -- M4 in "
        "source_offer_deployed_commit.json with the three tests that catch "
        "it missing from its pytest_target. That is exactly the shape this "
        "file exists to reject, so the rule is not doing its job."
    )
    assert any("record_deployed_commit.sh" in problem for problem in problems), (
        "the rejection must name the stamp script: " + "; ".join(problems)
    )


def test_the_fixed_filing_is_accepted_by_this_rule(test_files, observers_cache):
    """The same rule must ACCEPT the fixed shape, or it is simply broken.

    M4 now lives in `deployed_commit_script.json`, whose target contains the
    two tests that read the stamp script's bytes. This is the positive half
    of the negative control: a rule that rejects everything is not a guard
    either.
    """
    problems = problems_for(
        "deployed_commit_script.json",
        load_spec("deployed_commit_script.json"),
        observers_cache,
    )
    assert not problems, (
        "the rule rejected the FIXED filing, so it cannot be the rule that "
        "decides this:\n  " + "\n  ".join(problems)
    )


def test_m4_is_not_filed_under_a_spec_that_cannot_see_it(test_files, observers_cache):
    """M4 must not be back in a spec whose target cannot observe it."""
    spec = load_spec("source_offer_deployed_commit.json")
    for mutation in spec.get("mutations", []):
        assert mutation["file"] != M4_MUTANT["file"], (
            "M4 mutates ops/scripts/record_deployed_commit.sh, and this "
            "spec's pytest_target cannot observe that file (see the module "
            "docstring). It belongs in deployed_commit_script.json, whose "
            "target contains the tests that read the script's bytes."
        )


# --------------------------------------------------------------------------
# The rule itself: it has to be able to say NO
# --------------------------------------------------------------------------
def test_the_observability_check_can_say_no():
    """A detector that cannot say NO proves nothing.

    The check is exercised on the MEASURED ground truth of the defect, so the
    repository's own (currently correct) data cannot satisfy it: only a
    genuinely discriminating check can.
    """
    # A test that reads the file IS an observer.
    assert reads_file(
        REPO_ROOT / "tests" / "test_record_deployed_commit_script.py",
        "ops/scripts/record_deployed_commit.sh",
    ) is True, (
        "the check must recognise a test that reads the file's bytes"
    )
    # A test that only MENTIONS the file is NOT an observer. This is the
    # measured trap: test_update_sh_records_the_deployed_commit.py names
    # the stamp script and is completely blind to a defect in it.
    assert reads_file(
        REPO_ROOT / "tests" / "test_update_sh_records_the_deployed_commit.py",
        "ops/scripts/record_deployed_commit.sh",
    ) is False, (
        "a test that only mentions the file must not count as an observer: "
        "the mutation in that file is invisible to it (measured: rc == 0)"
    )
    # A test that reads a file it was HANDED is an observer, even when the
    # read happens inside a helper function. This is how the dotfile specs
    # read their subject.
    assert reads_file(
        REPO_ROOT / "tests" / "test_dockerignore_tracked_files.py",
        ".dockerignore",
    ) is True, (
        "a test that hands the path to a helper which reads it must count "
        "as an observer of that file"
    )
    # A test that walks the repository and reads every shell script, but
    # addresses only update.sh BY PATH, observes update.sh and NOT the other
    # scripts it happens to read at runtime.
    assert reads_file(
        REPO_ROOT / "tests" / "test_update_sh_never_writes_the_stamp.py",
        "update.sh",
    ) is True, (
        "a test that addresses a script by path must count as an observer "
        "of it"
    )
    assert reads_file(
        REPO_ROOT / "tests" / "test_update_sh_never_writes_the_stamp.py",
        "ops/scripts/smoke-test.sh",
    ) is False, (
        "a runtime-discovered path must not be attributed to a specific "
        "file: this test reads every .sh in the repository, but it names "
        "only update.sh by path, so it observes only that one"
    )
    # A file nobody reads is not an observer of anything.
    assert reads_file(
        REPO_ROOT / "tests" / "test_public_source_offer.py",
        "ops/scripts/record_deployed_commit.sh",
    ) is False, (
        "a test that neither reads nor runs the file must not count as an "
        "observer of it"
    )


def test_the_module_observability_check_can_say_no():
    """A Python module is observed by IMPORTING it, not by naming it."""
    assert imports_module(
        REPO_ROOT / "tests" / "test_api.py",
        "api/routes/diagnostics.py",
    ) is True, (
        "test_api.py imports api.routes.diagnostics, so it observes that "
        "module: importing a module EXECUTES it"
    )
    assert imports_module(
        REPO_ROOT / "tests" / "test_public_source_offer.py",
        "api/routes/diagnostics.py",
    ) is False, (
        "a test that does not import the module must not count as an "
        "observer of it"
    )


def test_a_mis_filed_mutant_is_rejected_by_the_rule(test_files, observers_cache):
    """A deliberately mis-filed mutant must be REJECTED, in isolation.

    This is the synthetic twin of the negative control above: a spec whose
    target cannot observe the file it mutates is rejected, and the same spec
    with an observer added is accepted.
    """
    mis_filed = {
        "pytest_target": "tests/test_public_source_offer.py",
        "mutations": [M4_MUTANT],
    }
    problems = coverage_problems("mis-filed", mis_filed, observers_cache)
    assert problems, (
        "a spec whose target cannot observe the file it mutates must be "
        "rejected: the mutant would survive for a filing reason"
    )
    assert any("record_deployed_commit.sh" in problem for problem in problems)

    now_observable = {
        "pytest_target": (
            "tests/test_public_source_offer.py "
            "tests/test_record_deployed_commit_script.py"
        ),
        "mutations": [M4_MUTANT],
    }
    assert not coverage_problems(
        "now-observable", now_observable, observers_cache
    ), (
        "the same spec, with a test that reads the mutated file in its "
        "target, must be ACCEPTED -- otherwise the rule rejects everything"
    )


# --------------------------------------------------------------------------
# The allowlist is a REVIEW, not a convenience
# --------------------------------------------------------------------------
def test_every_allowlist_entry_names_a_spec_that_exists():
    known = set(spec_files())
    for name, _reason, _date in ALLOWLIST:
        assert name in known, (
            f"the allowlist names a spec that does not exist: {name}. A stale "
            "entry hides a real problem behind a review that never happened."
        )


def test_every_allowlist_entry_carries_a_reason():
    for name, reason, _date in ALLOWLIST:
        assert reason.strip(), (
            f"the allowlist entry {name!r} carries no reason. A spec that "
            "cannot satisfy the rule has to say WHY, so that the exception "
            "is a decision and not an oversight."
        )


def test_the_allowlist_carries_a_review_stamp():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", ALLOWLIST_LAST_REVIEWED), (
        "the allowlist must carry the date it was last reviewed; changing it "
        "is a deliberate act"
    )
    for name, _reason, date in ALLOWLIST:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", date), (
            f"the allowlist entry {name!r} carries no review date: {date!r}"
        )


def test_no_spec_is_allowlisted_without_a_reason_being_stale(observers_cache):
    """An allowlisted spec must still FAIL R2, or the entry is stale.

    If a spec on the allowlist starts satisfying the rule, the exception has
    to be removed -- otherwise the allowlist grows into a place where real
    problems hide.
    """
    for name, reason, _date in ALLOWLIST:
        spec = load_spec(name)
        problems = completeness_problems(name, spec, observers_cache)
        assert problems, (
            f"{name} is on the allowlist but no longer has a test outside its "
            "pytest_target that observes one of the files it mutates. The "
            "entry is stale: remove it. Reason recorded: " + reason
        )


def test_every_specs_pytest_target_names_a_file_that_exists():
    """A target that does not exist makes the harness run nothing.

    `pytest` on a missing path errors out, and the harness reads that as an
    ABORT -- so a typo in a target would look like a mutation run that
    failed for an unrelated reason.
    """
    for name in spec_files():
        targets = target_of(load_spec(name))
        assert targets, f"{name} has an empty pytest_target"
        for target in targets:
            assert (REPO_ROOT / target).is_file(), (
                f"{name} names a file in its pytest_target that does not "
                f"exist: {target}"
            )


def test_this_file_is_not_in_any_specs_pytest_target():
    """This file reads the specs and the test sources, so it must not run
    DURING a mutation.

    It reads the MUTATED file's path out of the spec, and it reads the test
    sources; running it during a mutation would let it report on code that
    does not exist. The needle-audit file is held to the same rule.
    """
    for name in spec_files():
        targets = target_of(load_spec(name))
        assert THIS_FILE not in targets, (
            f"{THIS_FILE} reads the specs and the test sources, so it must "
            f"not be in the pytest_target of {name}: {targets}"
        )
