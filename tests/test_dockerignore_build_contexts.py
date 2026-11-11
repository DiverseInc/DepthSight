# tests/test_dockerignore_build_contexts.py
"""Every BUILD CONTEXT has to carry its own .dockerignore -- and carry the
same rule the root carries.

THE DEFECT THIS PINS
--------------------
MEASURED 2026-11-11 (review panel run #6, hermes, counted YES): `pwa/`
is built as its OWN docker build context (`context: ./pwa` in
docker-compose.yml) and `pwa/Dockerfile.prod` does `COPY . .` -- but `pwa/`
had NO .dockerignore, while `frontend/` HAS one. A docker build context uses
the .dockerignore in ITS OWN ROOT, so the root `.dockerignore` does not apply
to the pwa image at all. A gitignored file inside `pwa/` was therefore copied
into the pwa image, and `git status --porcelain` does not report ignored
files -- so the deploy stamp could not see it, and the published archive did
not contain it.

That makes the offer's promise false for that container: the offer says the
archive of the stamped commit "is the exact tree of the running build", and
the running build would carry a file the archive does not have.

IT IS NOT HYPOTHETICAL FOR A WEB BUILD: vite reads `.env` files at BUILD
time and INLINES their values into the shipped bundle, so a developer-local
`pwa/.env.local` would ship to users while being absent from the published
archive -- and it would never show up in a dirty-checkout warning.

WHAT WAS MEASURED ON PRODUCTION (2026-11-11): `pwa/.dockerignore` does not
exist on the host, and `pwa/` currently holds no gitignored file -- so the
hole was LATENT, not live. `frontend/` holds three `*.bak-*` files, which
land in the build stage and not in the final image. The fix is cheap and the
hole is one `touch` away from being live.

THE RULE
--------
For every build context whose Dockerfile copies the WHOLE context:

  * the context must have its OWN .dockerignore;
  * every path `.gitignore` excludes must be excluded by it -- at the
    context root, at depth 1 and at depth 2. This is the SAME rule the root
    context carries, and the same depth argument: git matches a bare
    pattern at ANY depth, docker matches it against the WHOLE relative path,
    so a bare rule covers the ROOT ONLY;
  * no TRACKED file may be excluded from it, except the small documented
    allowlist below.

A context whose Dockerfile copies NAMED paths only (`COPY scripts/ /opt/ops/`)
cannot pick up an unrelated file, so it does not need a rule set of its own.
That exemption is derived from the Dockerfile, never from a list -- so a
Dockerfile that starts copying everything is caught here.

THE PATHS ARE CONTEXT-RELATIVE
-------------------------------
A `.dockerignore` in `pwa/` is matched against paths relative to `pwa/`, so
`pwa/node_modules/x` is `node_modules/x` in that context. The rule has to be
asked with the path the builder actually sees, or it asks about a file that
does not exist in that context.

THE ALLOWLIST
-------------
The context's own `.gitignore` and its Dockerfiles may be excluded: the
builder reads them and the image does not need them. The ROOT context's own
allowlist lives in `tests/test_dockerignore_tracked_files.py`.
"""

import re
from pathlib import Path

import pytest
import yaml

from tests.test_dockerignore_covers_gitignore import (
    excluded_by,
    sample_paths_at_depths,
)
from tests.test_dockerignore_tracked_files import require_tracked_files

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
GITIGNORE_FILE = REPO_ROOT / ".gitignore"


def read_patterns(path: Path):
    """The rule set: the .dockerignore file with comments and blanks removed.

    This READ lives in THIS file on purpose. The mutation-observability guard
    (`tests/test_mutation_specs_must_be_observable_by_their_own_target.py`)
    decides whether this test can OBSERVE a change in a file by seeing which
    paths this file reads, and a read that happens inside another module is
    invisible to it -- which would make every mutant of these rule sets look
    like a survivor. (The MATCHER -- the part that decides what a pattern
    MEANS -- is the imported one, so there is still exactly one implementation
    of the semantics.)
    """
    out = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


# The build contexts that have their own context directory.
#
# Declared as module-level constants, and read through them, so the
# mutation-observability guard (`tests/test_mutation_specs_must_be_observable
# _by_their_own_target.py`) can resolve statically WHICH file each rule is
# about. A path computed at runtime resolves to nothing, and a guard that
# cannot see the file it guards is a guard that cannot fail.
FRONTEND_CONTEXT = "frontend"
OPS_CONTEXT = "ops"
PWA_CONTEXT = "pwa"
FRONTEND_DOCKERIGNORE = REPO_ROOT / "frontend" / ".dockerignore"
OPS_DOCKERIGNORE = REPO_ROOT / "ops" / ".dockerignore"
PWA_DOCKERIGNORE = REPO_ROOT / "pwa" / ".dockerignore"
FRONTEND_DOCKERFILE = REPO_ROOT / "frontend" / "Dockerfile.prod"
OPS_DOCKERFILE = REPO_ROOT / "ops" / "Dockerfile"
PWA_DOCKERFILE = REPO_ROOT / "pwa" / "Dockerfile.prod"

# The ROOT context. It CONTAINS frontend/ and pwa/ as subdirectories, and the
# root Dockerfile does `COPY . .` for SIX services (api, bot, celery_worker,
# market_data, mcp-memory, websocket) -- so the root context is a build
# context like any other, and it needs a rule set that covers the rules of
# the .gitignore files it CONTAINS.
ROOT_CONTEXT = "."
ROOT_DOCKERIGNORE = REPO_ROOT / ".dockerignore"

ALL_CONTEXTS = (FRONTEND_CONTEXT, OPS_CONTEXT, PWA_CONTEXT)

# Each context's OWN .gitignore. Git consults the NEAREST .gitignore, so a
# file these rules ignore is invisible to `git status --porcelain` even
# though it sits inside the build context -- which is why the forward rule
# has to be asked against them too.
CONTEXT_GITIGNORES = {
    FRONTEND_CONTEXT: REPO_ROOT / "frontend" / ".gitignore",
    PWA_CONTEXT: REPO_ROOT / "pwa" / ".gitignore",
}


def context_gitignore(context: str) -> Path:
    """The context's own .gitignore, or a path that does not exist."""
    return CONTEXT_GITIGNORES.get(context, REPO_ROOT / "no-such-context" / ".gitignore")


# --------------------------------------------------------------------------
# Which contexts exist, and what they copy
# --------------------------------------------------------------------------
def compose_build_contexts():
    """(context, dockerfile) for every service that builds an image."""
    data = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    services = data.get("services") or {}
    out = []
    for _name, svc in sorted(services.items()):
        build = (svc or {}).get("build")
        if not build:
            continue
        if isinstance(build, str):
            build = {"context": build}
        ctx = (build.get("context") or ".").strip()
        while ctx.startswith("./"):
            ctx = ctx[2:]
        ctx = ctx.rstrip("/") or "."
        out.append((ctx, build.get("dockerfile") or "Dockerfile"))
    return out


def copies_the_whole_context(dockerfile: Path) -> bool:
    """Does this Dockerfile copy the whole build context?

    `COPY . .` (and `COPY . /somewhere`) bring every file the context
    contains into the image. A Dockerfile that copies named paths only
    (`COPY scripts/ /opt/ops/`) cannot pick up an unrelated file, so it does
    not need the context's own .dockerignore.
    """
    if not dockerfile.is_file():
        return False
    for line in dockerfile.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^\s*COPY\s+(?:--from=\S+\s+)?(\S+)\s+(\S+)", line)
        if not m:
            continue
        if m.group(1) in (".", "./"):
            return True
    return False


# --------------------------------------------------------------------------
# Which .gitignore patterns can reach into a context
# --------------------------------------------------------------------------
def reachable_in(context: str, pattern: str) -> bool:
    """Can this `.gitignore` pattern match a path inside `context`?

    MEASURED against the real git (2026-11-11): git strips a TRAILING slash
    before it decides whether a pattern is anchored, so `build/` and `data/`
    match at ANY depth -- only a separator at the BEGINNING or in the MIDDLE
    anchors a pattern to the .gitignore's own directory. So:

      * a bare pattern matches at any depth, so it reaches into every
        context;
      * a pattern with an internal slash is anchored at the repository root,
        so it reaches into a context only when it NAMES that context.
    """
    if pattern.startswith("!"):
        return False
    body = pattern.rstrip("/")
    if body.startswith("/"):
        body = body[1:]
    if "/" in body:
        return body == context or body.startswith(context + "/")
    return True


def context_relative(context: str, sample: str) -> str:
    """The path the builder sees, for a path relative to the repository."""
    prefix = context + "/"
    if sample.startswith(prefix):
        return sample[len(prefix):]
    return sample


def uncovered_in_context(context: str, git_patterns, docker_patterns,
                         own_git_patterns=()):
    """(pattern, depth, sample) triples with no covering rule in the context.

    TWO .gitignore FILES APPLY INSIDE A CONTEXT, not one:

      * the ROOT's -- git consults every .gitignore up the tree, so the root's
        rules apply here as well, and a slashed root pattern reaches into a
        context only when it NAMES that context;
      * the CONTEXT'S OWN -- anchored at the context root, so EVERY one of its
        patterns applies inside the context.

    MEASURED 2026-11-11 (claude's residual on panel run #7, confirmed in
    code): this file checked ONLY the root's .gitignore. `frontend/.gitignore`
    ignores `*.local`, `dist-ssr`, `.DS_Store`, `*.suo`, `*.ntvs*`,
    `*.njsproj`, `*.sln`, `*.sw?` and `alembic/versions/*`, and NONE of those
    were covered by the context's rule set -- so such a file was copied into
    the image while `git status --porcelain` could not report it (git
    consults the NEAREST .gitignore, so the file was ignored and therefore
    unreported). A guard that is green on the case it was written for is
    worse than no guard: the case WAS in the repository the whole time.
    """
    uncovered = []
    for pattern in git_patterns:
        if pattern.startswith("!"):
            continue
        if not reachable_in(context, pattern):
            continue
        uncovered += _uncovered_for(pattern, docker_patterns, context)
    for pattern in (own_git_patterns or ()):
        if pattern.startswith("!"):
            continue
        uncovered += _uncovered_for(pattern, docker_patterns, context)
    return uncovered


def _uncovered_for(pattern: str, docker_patterns, context: str):
    out = []
    for depth, sample in sample_paths_at_depths(pattern):
        rel = context_relative(context, sample)
        if not excluded_by(docker_patterns, rel):
            out.append((pattern, depth, rel))
    return out


def allowed_exclusions(context: str, tracked) -> set:
    """The tracked files a context's own .dockerignore may exclude.

    The builder's own inputs, at the context root: the .gitignore and the
    Dockerfiles. They are read by the builder and are not runtime content.
    """
    allowed = set()
    for path in tracked:
        if not path.startswith(context + "/"):
            continue
        rel = path[len(context) + 1:]
        if "/" in rel:
            continue
        if rel == ".gitignore" or rel.startswith("Dockerfile"):
            allowed.add(path)
    return allowed


# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------
def test_every_build_context_is_declared_here():
    """A new build context must be added to this file.

    The contexts are DISCOVERED from docker-compose.yml, and the discovered
    set is compared with the declared one. If a service is given a context
    of its own, this test fails until the context is declared -- so a new
    context cannot quietly arrive without the rule being applied to it.
    """
    discovered = {ctx for ctx, _df in compose_build_contexts() if ctx != "."}
    declared = set(ALL_CONTEXTS)
    assert discovered == declared, (
        "the build contexts in docker-compose.yml and the contexts this file "
        "knows about differ: discovered=%s declared=%s. A context of its own "
        "needs its own rule set."
        % (sorted(discovered), sorted(declared))
    )


def test_a_context_that_copies_everything_has_its_own_dockerignore():
    """The hole itself, and it is derived from the Dockerfile."""
    for context, dockerfile, dockerignore in (
        (FRONTEND_CONTEXT, FRONTEND_DOCKERFILE, FRONTEND_DOCKERIGNORE),
        (OPS_CONTEXT, OPS_DOCKERFILE, OPS_DOCKERIGNORE),
        (PWA_CONTEXT, PWA_DOCKERFILE, PWA_DOCKERIGNORE),
    ):
        if not copies_the_whole_context(dockerfile):
            # The exemption is re-derived from the Dockerfile every time, so
            # a Dockerfile that starts copying everything is caught here
            # rather than being silently exempted by a list.
            continue
        assert dockerignore.is_file(), (
            "%s is its own docker build context and %s copies the whole "
            "context, but %s does not exist. The root .dockerignore does not "
            "apply to this context, so a gitignored file inside it is copied "
            "into the image while being absent from the published archive."
            % (context, dockerfile.name, dockerignore.name)
        )


def test_nothing_gitignored_in_the_context_enters_that_image():
    """The forward rule, per context, at depth 0, 1 and 2.

    It is asked against BOTH .gitignore files that apply inside the context:
    the root's (git consults every .gitignore up the tree) and the context's
    OWN (anchored at the context root). Checking only the root's is what
    let `*.local`, `dist-ssr` and the IDE files through -- see
    `uncovered_in_context`.
    """
    for context, dockerignore, docker_patterns in (
        (FRONTEND_CONTEXT, FRONTEND_DOCKERIGNORE, read_patterns(FRONTEND_DOCKERIGNORE)),
        (PWA_CONTEXT, PWA_DOCKERIGNORE, read_patterns(PWA_DOCKERIGNORE)),
    ):
        assert docker_patterns, "%s has no patterns" % dockerignore.name
        uncovered = uncovered_in_context(
            context, read_patterns(GITIGNORE_FILE), docker_patterns,
            own_git_patterns=read_patterns(context_gitignore(context)))
        assert not uncovered, (
            "these .gitignore patterns have no covering rule in %s, so a file "
            "they match is invisible to the deploy stamp (git status does not "
            "list ignored files) yet still copied into that image: %s"
            % (dockerignore.name,
               "; ".join("%s at depth %d (e.g. %s)" % t for t in uncovered[:12]))
        )


def test_no_tracked_file_is_swept_out_of_the_context():
    """The reverse rule: a tracked file must not vanish from the image."""
    tracked = require_tracked_files()
    for context, dockerignore, docker_patterns in (
        (FRONTEND_CONTEXT, FRONTEND_DOCKERIGNORE, read_patterns(FRONTEND_DOCKERIGNORE)),
        (PWA_CONTEXT, PWA_DOCKERIGNORE, read_patterns(PWA_DOCKERIGNORE)),
    ):
        inside = [p for p in tracked if p.startswith(context + "/")]
        excluded = [p for p in inside
                    if excluded_by(docker_patterns, p[len(context) + 1:])]
        allowed = allowed_exclusions(context, tracked)
        offenders = sorted(set(excluded) - allowed)
        assert not offenders, (
            "these TRACKED files are excluded from %s, so the image would not "
            "contain code the published archive does: %s"
            % (dockerignore.name, offenders[:12])
        )


def test_the_forward_check_can_fail():
    """The forward rule has to be able to say NO.

    It is exercised on SYNTHETIC pairs, so the repository's own (currently
    correct) data cannot satisfy it: a bare rule with no depth form must be
    reported as uncovered at depth 1 and 2.
    """
    uncovered = uncovered_in_context("pwa", ["*.pkl"], ["*.pkl"])
    assert uncovered, (
        "a bare rule covers the ROOT only, so the check must report the "
        "depth samples as uncovered"
    )
    assert {depth for _p, depth, _s in uncovered} == {1, 2}, (
        "the uncovered depths must be exactly 1 and 2"
    )
    assert uncovered_in_context("pwa", ["*.pkl"], ["*.pkl", "**/*.pkl"]) == [], (
        "adding the depth form must close the gap"
    )
    # and the context-relative path is the one the builder sees
    uncovered = uncovered_in_context("pwa", ["/pwa/node_modules/"], [])
    assert uncovered and uncovered[0][2].startswith("node_modules/"), (
        "the path must be asked in CONTEXT-RELATIVE form, because that is "
        "what the context's .dockerignore is matched against"
    )


def test_the_contexts_own_gitignore_is_asked_about():
    """The check must be able to say NO about the CONTEXT's own .gitignore.

    The negative control is the REAL class: `frontend/.gitignore` ignores
    `*.local`, and until 2026-11-11 nothing asked about it -- so a
    `frontend/x.local` was copied into the frontend image while
    `git status --porcelain` could not report it.
    """
    uncovered = uncovered_in_context("pwa", [], ["*.pkl"],
                                     own_git_patterns=["*.local"])
    assert uncovered, (
        "a pattern that only the CONTEXT'S OWN .gitignore carries must be "
        "asked about -- that is exactly the class this check used to skip"
    )
    assert {depth for _p, depth, _s in uncovered} == {0, 1, 2}, (
        "the uncovered depths must be exactly 0, 1 and 2"
    )
    assert uncovered_in_context("pwa", [], ["*.local", "**/*.local"],
                                own_git_patterns=["*.local"]) == [], (
        "adding the rule must close the gap"
    )


def repository_gitignores():
    """Every .gitignore in the repository, with the directory it governs.

    Git consults the NEAREST .gitignore, so the rules of `frontend/.gitignore`
    govern `frontend/*` even inside the ROOT build context. The directory
    each file governs is the prefix its patterns are relative to.
    """
    skip = ("node_modules", ".git", "site-packages", ".venv", "venv",
            "_parked", "__pycache__")
    out = []
    for g in sorted(REPO_ROOT.rglob(".gitignore")):
        rel = g.relative_to(REPO_ROOT).as_posix()
        parts = rel.split("/")
        if any(p in skip for p in parts):
            continue
        prefix = rel[: -len("/.gitignore")] if "/" in rel else ""
        out.append((prefix, read_patterns(g)))
    return out


def test_the_root_context_excludes_everything_the_repository_gitignores():
    """The root context CONTAINS frontend/ and pwa/, and git consults the
    NEAREST .gitignore -- so those directories' own rules apply inside the
    root context too.

    MEASURED 2026-11-11 (the same class as the pwa hole, found in the root):
    the root Dockerfile does `COPY . .` for SIX services, so `frontend/x.local`
    was copied into all six of those images while `git status --porcelain`
    could not report it (git consults the nearest .gitignore, so the file was
    ignored and therefore unreported).
    """
    docker_patterns = read_patterns(ROOT_DOCKERIGNORE)
    uncovered = []
    for prefix, git_patterns in repository_gitignores():
        for pattern in git_patterns:
            if pattern.startswith("!"):
                continue
            for depth, sample in sample_paths_at_depths(pattern):
                path = (prefix + "/" + sample) if prefix else sample
                if not excluded_by(docker_patterns, path):
                    uncovered.append((prefix or "<root>", pattern, depth, path))
    assert not uncovered, (
        "these .gitignore rules have no covering rule in the ROOT "
        ".dockerignore, so a file they match would be copied into the SIX "
        "images that build from the root context while being invisible to the "
        "deploy stamp: %s"
        % "; ".join("%s/%s at depth %d (e.g. %s)" % t for t in uncovered[:12])
    )


def test_the_root_context_check_can_fail():
    """The root check has to be able to say NO.

    It is exercised on a SYNTHETIC pair, so the repository's own (currently
    correct) data cannot satisfy it.
    """
    patterns = ["*.local"]
    samples = [(d, s) for d, s in sample_paths_at_depths("*.local")]
    path = "frontend/sub/deep/sample.local"
    assert not excluded_by(patterns, path), (
        "a bare rule covers the ROOT only, so a nested file is NOT excluded"
    )
    assert excluded_by(["**/frontend/**/*.local"], path), (
        "the scoped depth rule must cover the nested file inside the "
        "sub-directory"
    )
    assert excluded_by(["**/frontend/**/*.local"], "frontend/sample.local"), (
        "and the file directly inside the sub-directory"
    )
    assert samples, "the sampling must produce depths"


def test_the_allowlist_is_only_the_builders_own_inputs():
    tracked = [
        "pwa/.gitignore",
        "pwa/Dockerfile.prod",
        "pwa/Dockerfile.dev",
        "pwa/package.json",
        "pwa/.env.production",
        "pwa/src/index.tsx",
        "pwa/sub/Dockerfile.thing",
    ]
    allowed = allowed_exclusions("pwa", tracked)
    assert allowed == {
        "pwa/.gitignore", "pwa/Dockerfile.prod", "pwa/Dockerfile.dev"
    }, (
        "only the context's own .gitignore and its Dockerfiles may be "
        "excluded; a source file or an env file may not"
    )
