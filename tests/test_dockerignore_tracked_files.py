# tests/test_dockerignore_tracked_files.py
"""No .dockerignore pattern may sweep a TRACKED file out of the root build context.

THE DEFECT THIS PINS
--------------------
`tests/test_dockerignore_covers_gitignore.py` checks ONE direction: "every
pattern .gitignore excludes is also excluded by .dockerignore". The REVERSE
direction was never checked -- "does a .dockerignore pattern exclude a file
that git TRACKS?" -- and the review panel (dsh) flagged exactly that gap.

The two directions are not the same question. Excluding a tracked file from
the build context does NOT make the checkout dirty: `git status --porcelain`
does not report a tracked file as modified, so the deploy stamp still stamps
this checkout as "the committed tree" -- while the image is built WITHOUT
that file. The running image then differs from the published source archive,
which is precisely the promise the AGPL-3.0 section 13 offer makes ("the
archive is the exact tree of the running build").

MEASURED, NOT ASSUMED
---------------------
At this commit the repository tracks 1046 files. The .dockerignore rules
exclude exactly 273 of them from the ROOT build context:

    tests/                                                        254
    docs/                                                           5
    frontend/  (.gitignore, Dockerfile.dev, Dockerfile.prod,
                .env.production)                                     4
    pwa/      (the same four)                                       4
    .opencode/  (agents/audit.md, agents/frontend.md)               2
    .gitignore, Dockerfile, docker-compose.yml, ops/Dockerfile       4

and the DELTA introduced by this PR's .dockerignore change is ZERO tracked
files: none of the newly added patterns (*.sqlite, *.pkl, *.parquet,
*.sql, *.err, *.log, *.bak-*, credentials*.json, docker-compose.legacy.yml,
check.txt) matches a tracked file.

THE RULE
--------
No .dockerignore pattern may exclude a tracked file from the ROOT build
context, EXCEPT the files listed in the explicit allowlist below. The
excluded set must be EXACTLY that allowlist -- neither more (a new pattern
sweeping a tracked file out) nor fewer (an allowlist quietly shrunk) may pass
unnoticed.

WHY THE ROOT CONTEXT ONLY
-------------------------
The root .dockerignore applies to the services whose build context is the
repository ROOT (mcp-memory, api, websocket, bot, market_data, celery_worker
-- `build: .` in docker-compose.yml). `frontend` and `pwa` build from their
OWN contexts (./frontend, ./pwa) and `ops` from ./ops, so the root
.dockerignore does not apply to them. `frontend/.env.production` is excluded
from the ROOT context on purpose: it must never be baked into a root-context
image layer, while the frontend's own build still needs it.

THE ALLOWLIST IS A REVIEW, NOT A CONVENIENCE
---------------------------------------------
Every entry carries a reason and the allowlist carries a review stamp, so
adding an entry -- which is how you keep a tracked file out of the image --
is a deliberate, documented act.

THE MATCHER
-----------
Docker's own matcher is not callable from a test without a docker daemon, so
this file implements the subset of the syntax this repository uses: plain
paths, a leading "/" (anchored to the context root), a trailing "/" (a
directory), "*", "?", "**" and "!" (negation), with the LAST matching rule
winning, as Docker does. A reimplementation is only acceptable if it can
FAIL, so the matcher has its own controls: each one asserts that a pattern
does NOT match a path it must not match, and the rule test below asserts on
the COMPUTED excluded set -- not on a scan of the .dockerignore text.

THE COMPARISON IS A CALLABLE
-----------------------------
The verdict "the excluded set is EXACTLY the allowlist" lives in
`allowlist_matches_exactly`, not inline in a test. A verdict computed inline
can only ever be exercised on the repository's real, currently-correct data
-- where a subset check and an exact check agree -- so weakening it to a
subset check could not be detected by the data alone. Called directly with
deliberately inconsistent SYNTHETIC pairs, the comparison is itself under
test.
"""

import os
import re
import subprocess
from pathlib import Path
from typing import NamedTuple, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERIGNORE = REPO_ROOT / ".dockerignore"


# --------------------------------------------------------------------------
# The matcher
# --------------------------------------------------------------------------
class _Rule(NamedTuple):
    regex: str
    anchored: bool
    dir_only: bool
    negated: bool


def _glob_to_regex(glob: str) -> str:
    """The subset of Docker's glob syntax this repository uses.

    "*" matches within one path segment, "?" matches exactly one character
    and "**" crosses separators.
    """
    out = []
    i = 0
    n = len(glob)
    while i < n:
        if glob.startswith("**", i):
            starts_component = (i == 0) or glob[i - 1] == "/"
            j = i + 2
            ends_component = (j == n) or glob[j] == "/"
            if starts_component and ends_component:
                if j < n:
                    j += 1  # consume the '/' that follows '**'
                if i == 0 and j >= n:
                    out.append(".*")  # '**' alone matches everything
                elif i == 0:
                    out.append("(?:.*/)?")  # leading '**/' -> any depth
                elif j >= n:
                    out.append("(?:/.*)?")  # trailing '/**' -> everything below
                else:
                    out.append("(?:/.*/)?")  # 'a/**/b'
                i = j
                continue
            out.append(".*")
            i += 2
            continue
        c = glob[i]
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def _compile_pattern(pattern: str) -> Optional[_Rule]:
    """Compile one .dockerignore pattern, or None for a comment/blank line."""
    p = pattern.strip()
    if not p or p.startswith("#"):
        return None
    negated = p.startswith("!")
    if negated:
        p = p[1:]
    dir_only = p.endswith("/")
    if dir_only:
        p = p[:-1]
    anchored = p.startswith("/")
    if anchored:
        p = p[1:]
    elif "/" in p:
        anchored = True
    if not p:
        return None
    return _Rule(_glob_to_regex(p), anchored, dir_only, negated)


def _pattern_matches(rule: _Rule, path: str) -> bool:
    """Does this single rule match this path?"""
    segments = path.split("/")
    if rule.anchored:
        for k in range(1, len(segments) + 1):
            candidate = "/".join(segments[:k])
            if re.fullmatch(rule.regex, candidate):
                # A rule that matches a DIRECTORY covers everything under it.
                return k < len(segments) or not rule.dir_only
        return False
    for segment in segments:
        if re.fullmatch(rule.regex, segment):
            if rule.dir_only and segment == segments[-1]:
                continue  # 'data/' names a directory, not a file called data
            return True
    return False


def dockerignore_matches(pattern: str, path: str) -> bool:
    """Does a single .dockerignore pattern exclude this path?"""
    rule = _compile_pattern(pattern)
    if rule is None:
        return False
    return _pattern_matches(rule, path)


def excluded_by(patterns, path: str) -> bool:
    """Does the rule set exclude this path? The LAST matching rule wins."""
    matched = False
    excluded = False
    for pattern in patterns:
        rule = _compile_pattern(pattern)
        if rule is None:
            continue
        if _pattern_matches(rule, path):
            matched = True
            excluded = not rule.negated
    return matched and excluded


def read_patterns(path: Path):
    """The rule set: the .dockerignore file with comments and blanks removed."""
    out = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


# --------------------------------------------------------------------------
# The matcher's own controls: a matcher that cannot say NO proves nothing
# --------------------------------------------------------------------------
CONTROLS = [
    # a plain name matches at any level, and covers what is under it
    ("logs", "logs/app.log", True),
    ("logs", "a/logs/b.log", True),
    ("logs", "a/b/logs/c.log", True),
    ("logs", "log.txt", False),
    # a glob does not cross a separator, but matches at any depth
    ("*.py", "app.py", True),
    ("*.py", "a/b/app.py", True),
    ("*.py", "a/b/app.pyc", False),
    # a leading slash anchors to the context root
    ("/root.txt", "root.txt", True),
    ("/root.txt", "a/root.txt", False),
    # a directory rule covers its contents
    ("a/b", "a/b/c.txt", True),
    ("a/b", "a/b", True),
    # a trailing slash means a directory
    ("data/", "data/x", True),
    ("data/", "a/data/x", True),
    ("data/", "database/x", False),
    # '**' crosses separators
    ("**/secrets/", "a/b/secrets/k", True),
    ("**/*.env", "a/b/.env", True),
    # '?' is exactly one character
    ("app?.py", "app1.py", True),
    ("app?.py", "app12.py", False),
    # the shapes this repository actually uses
    (".env", ".env", True),
    (".env", ".envx", False),
    (".env.*", "frontend/.env.production", True),
    ("Dockerfile.*", "frontend/Dockerfile.dev", True),
    ("tests/", "tests/unit/test_x.py", True),
    # the negative control: a file nothing matches SURVIVES
    ("*.txt", "app.py", False),
]


@pytest.mark.parametrize("pattern,path,want", CONTROLS)
def test_the_matcher_can_say_no(pattern, path, want):
    assert dockerignore_matches(pattern, path) is want, (
        f"the matcher got {pattern!r} vs {path!r} wrong; a matcher that "
        "matches everything would make the rule test below meaningless"
    )


def test_the_matcher_honours_negation_and_the_last_matching_rule_wins():
    """`!keep.txt` re-includes a file; a later rule overrides an earlier one."""
    assert excluded_by(["*.txt", "!keep.txt"], "keep.txt") is False, (
        "the '!keep.txt' negation must re-include the file"
    )
    assert excluded_by(["!keep.txt", "*.txt"], "keep.txt") is True, (
        "the LAST matching rule wins, as Docker does"
    )
    assert excluded_by(["*.txt"], "app.py") is False, (
        "the negative control: a file that nothing matches must survive"
    )


# --------------------------------------------------------------------------
# The tracked file list
#
# WHY THE RESOLUTION IS NOT A ONE-LINER
# -------------------------------------
# `git ls-files` reads the INDEX: it is purely local and never touches the
# network, so neither the tracked-file list nor the tracked-file COUNT can
# be affected by an offline machine.
#
# But WHICH repository the index belongs to is environment-dependent, and a
# SILENT failure here is VACUOUS: the tracked-file list comes back empty,
# every rule test below stops looking at anything, and the suite still
# reports green. That is exactly how the mutation harness recorded M1, M2,
# M3, M6 and M7 as SURVIVORS -- the product WAS defended, the guard was
# SILENT. Measured: under WSL the run was "28 passed, 4 skipped", and
# pytest exits 0 when the only outcomes are skips.
#
# This checkout is a LINKED WORKTREE: its .git is a FILE holding one line
#     gitdir: C:/dev/DepthSight/.git/worktrees/DepthSight-sourceoffer
# and WSL's git resolves that Windows drive path RELATIVE to the current
# directory, so `git ls-files` fails with rc=128 ("fatal: not a git
# repository: .../C:/dev/..."). The list is therefore also resolved through
# the worktree's git dir given EXPLICITLY, with the Windows drive path
# translated to the WSL mount point (/mnt/c/...) as well.
# --------------------------------------------------------------------------
def _wsl_path(path: str) -> str:
    """A Windows drive path as WSL can use it: C:/dev/x -> /mnt/c/dev/x.

    WSL mounts the C: drive at /mnt/c. A linked worktree's .git FILE holds
    a Windows drive path, and WSL's git cannot find it unless it is
    translated.
    """
    if len(path) >= 3 and path[1] == ":" and path[0].isalpha():
        return "/mnt/" + path[0].lower() + path[2:].replace("\\", "/")
    return path


def _git_dir_candidates():
    """The git directories that may hold this checkout's index.

    A plain checkout has a .git DIRECTORY. A LINKED worktree has a .git
    FILE with one `gitdir:` line, and THAT is the directory to ask git for:
    it holds this worktree's own index.
    """
    dot_git = REPO_ROOT / ".git"
    if dot_git.is_dir():
        return [str(dot_git)]
    if not dot_git.is_file():
        return []
    try:
        text = dot_git.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        if not line.strip().lower().startswith("gitdir:"):
            continue
        raw = line.split(":", 1)[1].strip()
        if not raw:
            continue
        candidates = [raw]
        if not os.path.isabs(raw):
            # a relative gitdir is relative to the worktree directory
            candidates.append(str(REPO_ROOT / raw))
        candidates.append(_wsl_path(raw))
        return candidates
    return []


def tracked_files():
    """The files git tracks, or None if git cannot answer here.

    Every candidate is tried in turn: the plain listing; the
    `safe.directory` variant (the mutation harness runs pytest under WSL,
    where the Windows-mounted repository is owned by another user and git
    refuses to operate on it unless that is allowed); and the listing
    through the worktree's git dir given EXPLICITLY, which is the only form
    that works under WSL, where the .git file holds a Windows drive path.
    """
    base = ["git", "-C", str(REPO_ROOT), "ls-files", "-z"]
    fallback = ["git", "-c", "safe.directory=*", "-C", str(REPO_ROOT),
                "ls-files", "-z"]
    commands = [base, fallback]
    for git_dir in _git_dir_candidates():
        explicit = ["git", f"--git-dir={git_dir}",
                    f"--work-tree={REPO_ROOT}", "ls-files", "-z"]
        commands.append(explicit)
        commands.append(["git", "-c", "safe.directory=*",
                         f"--git-dir={git_dir}", f"--work-tree={REPO_ROOT}",
                         "ls-files", "-z"])
    for cmd in commands:
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode != 0:
            continue
        names = [f for f in out.stdout.decode("utf-8", "replace").split("\0") if f]
        if names:
            return names
    return None


def require_tracked_files(resolver=tracked_files):
    """The tracked-file list, or a LOUD failure.

    A checkout whose tracked-file list cannot be resolved is a FAILURE,
    never a skip. A skip makes every rule test below VACUOUS while the
    suite still reports green -- which is precisely what let M1, M2, M3, M6
    and M7 through the mutation harness. The repo convention is the sibling
    guard tests/test_gitignore_ignores_env_copies.py, which raises
    "git ls-files failed ... the tracked-file check would be vacuous".

    A skip is correct in exactly one case: there is no .git here at all, so
    this is not a checkout and there is no tracked-file list to check.
    """
    tracked = resolver()
    if tracked is None:
        if not (REPO_ROOT / ".git").exists():
            pytest.skip(
                "this is not a git checkout (there is no .git here), so there "
                "is no tracked-file list to check"
            )
        raise AssertionError(
            "git ls-files failed in this checkout: the tracked-file list is "
            "unavailable, so the .dockerignore tracked-file rules would be "
            "vacuous. See tests/test_gitignore_ignores_env_copies.py for the "
            "same guard."
        )
    return tracked


# --------------------------------------------------------------------------
# THE ALLOWLIST: what may be excluded from the ROOT context even though git
# tracks it.  Every entry carries a reason: adding an entry is how you keep
# a tracked file out of the image, so it has to be a deliberate act.
# --------------------------------------------------------------------------
ALLOWLIST = (
    ("dir", "tests/",
     "the runtime services never import the test suite (alembic needs "
     "alembic/ + alembic.ini); the AGPL offer publishes it in the archive"),
    ("dir", "docs/",
     "documentation is not needed at runtime; the AGPL offer publishes it in "
     "the source archive"),
    ("dir", ".opencode/",
     "local agent transcripts and tool output, not product code"),
    ("file", ".gitignore",
     "the VCS ignore list is not needed in the image"),
    ("file", "Dockerfile",
     "the image is built FROM this Dockerfile; it does not need itself"),
    ("file", "docker-compose.yml",
     "compose is read on the HOST, not inside the image"),
    ("file", "ops/Dockerfile",
     "the ops image builds from ./ops, so the root context does not need it"),
    ("file", "frontend/.gitignore",
     "the frontend image builds from ./frontend (its own context), so the "
     "root context does not need it"),
    ("file", "frontend/Dockerfile.dev",
     "the frontend image builds from ./frontend (its own context), so the "
     "root context does not need it"),
    ("file", "frontend/Dockerfile.prod",
     "the frontend image builds from ./frontend (its own context), so the "
     "root context does not need it"),
    ("file", "frontend/.env.production",
     "the frontend's own build (./frontend) needs it, and it must NEVER be "
     "baked into a ROOT-context image layer"),
    ("file", "pwa/.gitignore",
     "the pwa image builds from ./pwa (its own context), so the root context "
     "does not need it"),
    ("file", "pwa/Dockerfile.dev",
     "the pwa image builds from ./pwa (its own context), so the root context "
     "does not need it"),
    ("file", "pwa/Dockerfile.prod",
     "the pwa image builds from ./pwa (its own context), so the root context "
     "does not need it"),
    ("file", "pwa/.env.production",
     "the pwa's own build (./pwa) needs it, and it must NEVER be baked into "
     "a ROOT-context image layer"),
)

# Bump this when the allowlist changes: a change to it is a REVIEW, not a
# side effect of editing a pattern.
ALLOWLIST_LAST_REVIEWED = "2026-11-09"


def allowlist_expected(tracked):
    """The tracked files the allowlist permits to be excluded."""
    expected = set()
    for kind, value, _reason in ALLOWLIST:
        if kind == "dir":
            expected.update(f for f in tracked if f.startswith(value))
        else:
            expected.add(value)
    return expected


def excluded_tracked_files(tracked, patterns):
    """The tracked files the .dockerignore rules exclude from the ROOT context."""
    return sorted(f for f in tracked if excluded_by(patterns, f))


def allowlist_matches_exactly(allowlist, excluded) -> bool:
    """Are the excluded tracked files EXACTLY the allowlist?

    BOTH directions matter, and a subset check is not good enough:

      * excluded \\ allowlist -- a tracked file left the image: the running
        build no longer matches the published archive.
      * allowlist \\ excluded -- an allowlist entry went stale, so the
        documented set and the real set have drifted apart.

    This comparison is a CALLABLE on purpose. A verdict computed inline can
    only ever be exercised on the repository's real, currently-correct data,
    where a subset check and an exact check agree -- so weakening it to a
    subset check could not be detected. Called directly with deliberately
    inconsistent SYNTHETIC pairs, the comparison itself is under test and a
    weakening of it goes red.
    """
    return set(allowlist) == set(excluded)


# --------------------------------------------------------------------------
# The rule
#
# Every test below reads the tracked-file list through require_tracked_files(),
# which FAILS LOUDLY when the list cannot be resolved in a checkout. A skip
# here would leave the suite green with nothing checked.
# --------------------------------------------------------------------------
def test_the_tracked_file_list_is_real_and_local():
    tracked = require_tracked_files()
    assert len(tracked) >= 1000, (
        f"git reported only {len(tracked)} tracked files; the measured count "
        "at this commit is 1046, so an empty or truncated list would make "
        "every rule test below vacuous"
    )
    for anchor in ("Dockerfile", "docker-compose.yml", ".gitignore",
                   "tests/test_dockerignore_covers_gitignore.py"):
        assert anchor in tracked, (
            f"{anchor} is not in the tracked-file list, so the list is not "
            "the repository's index"
        )


def test_the_tracked_file_list_is_obtainable_here_and_never_fails_silently():
    """The GUARD itself is under test.

    Two things have to hold for every rule test below to mean anything:

      1. the tracked-file list IS obtainable in this checkout, and
      2. when it is NOT obtainable, the failure is LOUD -- an AssertionError,
         never a skip.

    (2) is the half that was missing. The mutation harness runs pytest under
    WSL, where the linked worktree's .git file holds a Windows drive path
    and `git ls-files` fails with rc=128. The four rule tests then SKIPPED,
    pytest exited 0, and the harness recorded M1, M2, M3, M6 and M7 as
    SURVIVORS: the product was defended and the guard was SILENT.

    The broken resolution is injected directly, so this test does not depend
    on the repository's git state to be able to fail.
    """
    if not (REPO_ROOT / ".git").exists():
        # The AGPL source archive ships tests/ but no .git, so the published
        # suite has to stay green when it is unpacked outside a checkout. There
        # is no tracked-file list to check there, so there is nothing to guard.
        pytest.skip(
            "this test only means something in a checkout; there is no .git here"
        )
    tracked = require_tracked_files()
    assert tracked, (
        "the tracked-file list came back empty, so every rule test below "
        "would be vacuous"
    )

    # A broken resolution must FAIL. pytest.skip raises a BaseException, not
    # an AssertionError, so a skip has to be caught explicitly here: left
    # alone it would escape this test, be reported as a skip, and pytest
    # would exit 0 -- a green run that checked nothing.
    try:
        require_tracked_files(resolver=lambda: None)
    except AssertionError:
        pass
    except BaseException as exc:
        raise AssertionError(
            "a broken tracked-file resolution must FAIL loudly, but it "
            f"raised {type(exc).__name__} instead of an AssertionError"
        ) from exc
    else:
        raise AssertionError(
            "a broken tracked-file resolution returned silently instead of "
            "failing loudly; a silent failure makes every rule test vacuous"
        )


def test_no_dockerignore_pattern_excludes_a_tracked_file_outside_the_allowlist():
    """The REVERSE direction: a tracked file may not leave the root context."""
    tracked = require_tracked_files()
    patterns = read_patterns(DOCKERIGNORE)
    excluded = excluded_tracked_files(tracked, patterns)
    allowed = allowlist_expected(tracked)

    offenders = [f for f in excluded if f not in allowed]
    assert not offenders, (
        f"{len(offenders)} tracked file(s) are excluded from the ROOT build "
        "context by .dockerignore but are NOT on the allowlist. A tracked "
        "file that is excluded from the build context does not make the "
        "checkout dirty (git status does not list it), so the deploy stamp "
        "still calls this the committed tree -- while the image is built "
        "without the file and the running image differs from the published "
        "source archive: " + ", ".join(offenders[:20])
    )


def test_the_allowlist_is_exactly_the_excluded_set_neither_more_nor_fewer():
    """Both directions: nothing new excluded, and no allowlist entry stale."""
    tracked = require_tracked_files()
    patterns = read_patterns(DOCKERIGNORE)
    excluded = set(excluded_tracked_files(tracked, patterns))
    allowed = allowlist_expected(tracked)

    assert allowlist_matches_exactly(allowed, excluded), (
        "the set of tracked files excluded from the ROOT build context is not "
        "exactly the allowlist.\n"
        f"excluded but not on the allowlist ({len(excluded - allowed)}): "
        + ", ".join(sorted(excluded - allowed)[:20]) + "\n"
        f"on the allowlist but not excluded ({len(allowed - excluded)}): "
        + ", ".join(sorted(allowed - excluded)[:20]) + "\n"
        "a new exclusion sweeps a tracked file out of the image; a shrunken "
        "allowlist hides one."
    )


def test_the_exactness_comparison_rejects_both_directions():
    """The COMPARISON itself is under test, on synthetic sets.

    The comparison is a callable so it can be called directly. The pairs
    below are SYNTHETIC: they do not come from the repository's tracked
    files, so the product state cannot satisfy this test -- only a genuinely
    exact comparison can.
    """
    # equal sets: exactly matching
    assert allowlist_matches_exactly({"a.txt", "b.txt"}, {"a.txt", "b.txt"}) is True, (
        "equal sets must match exactly"
    )
    # the excluded set is missing an allowlist entry (a strict subset)
    assert allowlist_matches_exactly({"a.txt", "b.txt"}, {"a.txt"}) is False, (
        "a subset comparison would let a silently shrunken allowlist pass; "
        "the excluded set is missing 'b.txt', so it is NOT an exact match"
    )
    # the excluded set has a file the allowlist does not
    assert allowlist_matches_exactly({"a.txt"}, {"a.txt", "b.txt"}) is False, (
        "a subset comparison would let a new exclusion pass; the excluded "
        "set has 'b.txt', which the allowlist does not, so it is NOT an "
        "exact match"
    )
    # disjoint sets
    assert allowlist_matches_exactly({"a.txt"}, {"b.txt"}) is False, (
        "disjoint sets are not an exact match"
    )
    # empty is only equal to empty
    assert allowlist_matches_exactly(set(), set()) is True, (
        "two empty sets are exactly equal"
    )
    assert allowlist_matches_exactly(set(), {"a.txt"}) is False, (
        "an empty allowlist does not exactly match a non-empty excluded set"
    )
    assert allowlist_matches_exactly({"a.txt"}, set()) is False, (
        "a non-empty allowlist does not exactly match an empty excluded set"
    )


def test_every_allowlist_entry_is_still_exercised():
    """No stale entry: each allowlist entry must still exclude something."""
    tracked = require_tracked_files()
    patterns = read_patterns(DOCKERIGNORE)
    excluded = set(excluded_tracked_files(tracked, patterns))

    stale = []
    for kind, value, _reason in ALLOWLIST:
        if kind == "dir":
            covered = [f for f in tracked if f.startswith(value) and f in excluded]
        else:
            covered = [value] if value in excluded else []
        if not covered:
            stale.append(f"{kind}:{value}")
    assert not stale, (
        "these allowlist entries no longer exclude any tracked file, so the "
        "allowlist and the .dockerignore have drifted apart: " + ", ".join(stale)
    )


def test_every_allowlist_entry_carries_a_reason():
    for kind, value, reason in ALLOWLIST:
        assert reason.strip(), (
            f"the allowlist entry {value!r} carries no reason; keeping a "
            "tracked file out of the image is a decision that has to be "
            "written down"
        )


def test_the_allowlist_carries_a_review_stamp():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", ALLOWLIST_LAST_REVIEWED), (
        "the allowlist must carry the date it was last reviewed; changing it "
        "is a deliberate act"
    )
