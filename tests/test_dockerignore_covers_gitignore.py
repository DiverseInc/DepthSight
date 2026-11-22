# tests/test_dockerignore_covers_gitignore.py
"""Nothing that .gitignore excludes may enter the built image -- AT ANY DEPTH.

THE DEFECT THIS PINS
--------------------
The image is built from the Docker build CONTEXT (`COPY . .` in the root
Dockerfile), while the AGPL-3.0 section 13 offer publishes the COMMITTED
tree. The deploy stamp refuses to stamp a checkout that is not the committed
tree, and it decides that with `git status --porcelain` -- which does not
list IGNORED files.

So a file that git ignores is invisible to the stamp (the checkout looks
clean) and still copied into the image. The running image then contains
files the published archive does not, which makes the offer's central
promise -- "the archive is the exact tree of the running build" -- false.

THE DEPTH HOLE (measured with the real docker CLI, not inferred)
------------------------------------------------------------------
GIT and DOCKER disagree about what a bare pattern means:

  * git: a `.gitignore` pattern with no slash matches at ANY depth, so
    `*.pkl` ignores `sub/model.pkl`;
  * docker: `*` compiles to `[^/]*` and the pattern is matched against the
    WHOLE relative path, so a bare pattern covers the ROOT ONLY. `*.pkl`
    does NOT match `sub/model.pkl`; `**/*.pkl` does.

MEASURED: with this repository's `.dockerignore`, a scratch image built
from `FROM scratch` + `COPY . /ctx` carried ALL TEN of these nested files
into the image -- `sub/model.pkl`, `sub/data.sql`, `sub/train.log`,
`sub/train.log.1`, `sub/db.sqlite`, `sub/notes.err`, `sub/weights.parquet`,
`sub/scratch/notes.txt`, `sub/check.txt`, `sub/backup.bak-1` -- while the
four root-level equivalents were correctly excluded. `git check-ignore`
confirms git hides all ten.

The pinned tests did not catch it because BOTH .dockerignore matchers in this
repository implemented GIT's semantics for a bare pattern. The forward test
below believed the coverage existed; the reverse test
(`tests/test_dockerignore_tracked_files.py`) computed its excluded set with
the same over-broad matcher. The matchers here now implement DOCKER's
semantics, measured against the real docker CLI.

THE RULE
--------
Every pattern `.gitignore` excludes must also be excluded by
`.dockerignore` -- at the ROOT, at depth 1 and at depth 2. That is the
invariant that makes the offer's promise true by construction rather than by
luck.

THE OTHER DIRECTION
-------------------
That rule is ONE direction. The reverse -- "does a `.dockerignore` pattern
exclude a file that git TRACKS?" -- is a different question, and it was not
checked here: a tracked file excluded from the build context does not make
the checkout dirty, so the deploy stamp still calls it the committed tree
while the image is built without it. That reverse direction, and the
explicit allowlist of the tracked files that may be excluded, are pinned in
`tests/test_dockerignore_tracked_files.py`.

THE MATCHERS
------------
Docker's own pattern matcher is not callable from a test without a docker
daemon, so this file implements the subset of the syntax this repository
uses -- plain names, `*` (not crossing `/`), `**` (crossing `/`), `?`, a
leading `/` (ignored by Docker) and `!` (re-includes).

A reimplementation is only acceptable if it can FAIL, so both matchers have
their own controls: each one asserts that a pattern does NOT match a path it
must not match. A matcher that matches everything would pass the rule test
and is caught by the controls.

The GIT matcher is here for the .gitignore side of the rule, and it is NOT
Docker's matcher: git matches a bare pattern at any depth. Mixing the two up
is exactly the defect this file now pins, so the difference is asserted
explicitly in
`test_the_two_matchers_disagree_about_a_bare_pattern_and_that_is_the_point`.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GITIGNORE = REPO_ROOT / ".gitignore"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"


# --------------------------------------------------------------------------
# The matcher: DOCKER's semantics
# --------------------------------------------------------------------------
def _glob_to_regex(glob: str) -> str:
    """Docker's glob syntax, compiled against the WHOLE relative path.

    "*" matches within one path segment, "?" matches exactly one character,
    and "**" crosses separators. "**/" matches any number of leading
    directories, including none -- which is why "**/logs" matches "logs" and
    "sub/logs" alike.
    """
    out = []
    i = 0
    n = len(glob)
    while i < n:
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if glob.startswith("**", i):
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


def _compile_docker_pattern(pattern: str):
    """(regex, negated) for one .dockerignore pattern, or None for a comment.

    MEASURED with the real docker CLI: a leading AND a trailing slash are
    IGNORED -- "data/" behaves exactly like "data", and "data/" even matches
    a FILE named "data". There is no "directory only" rule in Docker's
    .dockerignore, so none is implemented here.
    """
    p = pattern.strip()
    if not p or p.startswith("#"):
        return None
    negated = p.startswith("!")
    if negated:
        p = p[1:]
    p = p.strip("/")
    if not p:
        return None
    return _glob_to_regex(p), negated


def _docker_regex_matches(regex: str, path: str) -> bool:
    """Does the compiled rule match the path, or a directory above it?

    A rule that matches a DIRECTORY covers everything under it: "logs"
    excludes "logs/app.log", and "test*" excludes "tests/unit/test_x.py".
    """
    if re.fullmatch(regex, path):
        return True
    segments = path.split("/")
    for k in range(1, len(segments)):
        if re.fullmatch(regex, "/".join(segments[:k])):
            return True
    return False


def dockerignore_matches(pattern: str, path: str) -> bool:
    """Does a single .dockerignore pattern exclude this path?"""
    compiled = _compile_docker_pattern(pattern)
    if compiled is None:
        return False
    regex, _negated = compiled
    return _docker_regex_matches(regex, path)


def excluded_by(patterns, path: str) -> bool:
    """Does the RULE SET exclude this path? The LAST matching rule wins.

    Docker applies the rules in order and the last matching one decides, so a
    trailing "!pattern" re-includes a file an earlier rule excluded.
    """
    matched = False
    excluded = False
    for pattern in patterns:
        compiled = _compile_docker_pattern(pattern)
        if compiled is None:
            continue
        regex, negated = compiled
        if _docker_regex_matches(regex, path):
            matched = True
            excluded = not negated
    return matched and excluded


# --------------------------------------------------------------------------
# The matcher: GIT's semantics (for the .gitignore side of the rule)
# --------------------------------------------------------------------------
def gitignore_matches(pattern: str, path: str) -> bool:
    """Does a single .gitignore pattern ignore this path?

    GIT is not Docker: a pattern with no slash matches at ANY depth, while a
    pattern containing a slash -- or a leading slash -- is anchored to the
    directory that holds the .gitignore file (the repository root here).
    """
    p = pattern.strip()
    if not p or p.startswith("#"):
        return False
    if p.startswith("!"):
        p = p[1:]
    p = p.rstrip("/")
    anchored = p.startswith("/") or ("/" in p)
    if p.startswith("/"):
        p = p[1:]
    if not p:
        return False
    regex = _glob_to_regex(p)
    if anchored:
        # An anchored pattern that names a DIRECTORY covers what is under it.
        if re.fullmatch(regex, path):
            return True
        segments = path.split("/")
        for k in range(1, len(segments)):
            if re.fullmatch(regex, "/".join(segments[:k])):
                return True
        return False
    # A bare pattern matches at any depth.
    for segment in path.split("/"):
        if re.fullmatch(regex, segment) is not None:
            return True
    return False


def git_excluded_by(patterns, path: str) -> bool:
    """Does the .gitignore RULE SET ignore this path? Last matching rule wins."""
    matched = False
    ignored = False
    for pattern in patterns:
        p = pattern.strip()
        if not p or p.startswith("#"):
            continue
        negated = p.startswith("!")
        if gitignore_matches(p, path):
            matched = True
            ignored = not negated
    return matched and ignored


# --------------------------------------------------------------------------
# Reading the two rule sets
# --------------------------------------------------------------------------
def read_patterns(path: Path) -> list:
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


# --------------------------------------------------------------------------
# Sampling a .gitignore pattern at the root and at depth
# --------------------------------------------------------------------------
#: The prefixes the same path is sampled at: the root, depth 1 and depth 2.
DEPTH_PREFIXES = ("", "sub/", "sub/deep/")


def sample_path_for(pattern: str) -> str:
    """A concrete path the gitignore pattern would match, at the ROOT.

    A pattern that names a DIRECTORY is sampled as a file inside it, so the
    sample is unambiguous: "logs/" becomes "logs/x".
    """
    p = pattern.strip()
    is_dir = p.endswith("/")
    p = p.rstrip("/")
    if p.startswith("/"):
        p = p[1:]
    if p.startswith("**/"):
        p = p[3:]
    p = p.replace("**/", "")
    s = re.sub(r"\*\*", "deep", p)
    s = re.sub(r"\*", "sample", s)
    s = re.sub(r"\?", "x", s)
    if not s:
        return ""
    return s + "/x" if is_dir else s


def sample_paths_at_depths(pattern: str) -> list:
    """The same path at depth 0, depth 1 and depth 2.

    Docker matches a bare pattern against the WHOLE relative path, so the
    root sample and the depth samples are DIFFERENT questions: a bare
    ".dockerignore" rule answers the first and not the second.
    """
    base = sample_path_for(pattern)
    if not base:
        return []
    return [(depth, prefix + base)
            for depth, prefix in enumerate(DEPTH_PREFIXES)]


def uncovered_patterns(git_patterns, docker_patterns) -> list:
    """The (pattern, depth, sample) triples with no covering .dockerignore rule.

    This is the RULE as a callable, so it can be exercised on synthetic
    pairs: a verdict computed inline could only ever be exercised on the
    repository's real, currently-correct data.
    """
    uncovered = []
    for pattern in git_patterns:
        if pattern.startswith("!"):
            continue  # a negation UN-ignores: that path is tracked, so it
            # belongs in the image with the rest of the committed tree.
        for depth, sample in sample_paths_at_depths(pattern):
            if not excluded_by(docker_patterns, sample):
                uncovered.append((pattern, depth, sample))
    return uncovered


# --------------------------------------------------------------------------
# The matchers' own controls: a matcher that cannot say NO is not a matcher
# --------------------------------------------------------------------------
DOCKER_CONTROLS = [
    # a bare pattern covers the ROOT ONLY: docker compiles '*' to '[^/]*' and
    # matches the pattern against the WHOLE relative path
    ("*.pkl", "model.pkl", True),
    ("*.pkl", "sub/model.pkl", False),
    ("*.pkl", "sub/deep/model.pkl", False),
    ("check.txt", "check.txt", True),
    ("check.txt", "sub/check.txt", False),
    # '**/' crosses directories, and also matches at the root
    ("**/*.pkl", "model.pkl", True),
    ("**/*.pkl", "sub/model.pkl", True),
    ("**/*.pkl", "sub/deep/model.pkl", True),
    ("**/logs", "logs/app.log", True),
    ("**/logs", "sub/logs/app.log", True),
    # a rule that matches a DIRECTORY covers everything under it
    ("logs", "logs/app.log", True),
    ("logs", "sub/logs/app.log", False),
    ("test*", "tests/unit/test_x.py", True),
    # a pattern containing a slash is anchored to the context root
    ("/root.txt", "root.txt", True),
    ("/root.txt", "a/root.txt", False),
    ("a/b", "a/b/c.txt", True),
    ("a/b", "x/a/b/c.txt", False),
    # a trailing slash is IGNORED by docker: "data/" is "data"
    ("data/", "data/x", True),
    ("data/", "database/x", False),
    # '?' is exactly one character
    ("app?.py", "app1.py", True),
    ("app?.py", "app12.py", False),
    # the shapes this repository actually uses
    (".env", ".env", True),
    (".env", ".envx", False),
    (".env", "a/.env", False),
    (".env.*", ".env.backup", True),
    (".env.*", ".env_backup", False),
    (".env.*", ".env-backup", False),
    ("**/.env.*", "frontend/.env.production", True),
    ("Dockerfile.*", "frontend/Dockerfile.dev", False),
    ("**/Dockerfile*", "frontend/Dockerfile.dev", True),
    # the negative control: a file nothing matches SURVIVES
    ("*.txt", "app.py", False),
]

GIT_CONTROLS = [
    # a bare gitignore pattern matches at ANY depth -- the opposite of docker
    ("*.pkl", "model.pkl", True),
    ("*.pkl", "sub/model.pkl", True),
    ("*.pkl", "sub/deep/model.pkl", True),
    ("check.txt", "sub/check.txt", True),
    (".env_*", "a/.env_backup", True),
    (".env-*", "a/.env-backup", True),
    (".env.*", "a/.env.backup", True),
    # a pattern containing a slash is anchored to the .gitignore's directory
    ("/md/", "md/x", True),
    ("/md/", "sub/md/x", False),
    ("depth_sight_hft/*.log", "depth_sight_hft/train.log", True),
    # a directory pattern matches the files inside it
    ("logs/", "sub/logs/app.log", True),
    # the negative control: a file nothing matches SURVIVES
    ("*.pkl", "app.py", False),
]


@pytest.mark.parametrize("pattern,path,want", DOCKER_CONTROLS)
def test_the_docker_matcher_can_say_no(pattern, path, want):
    assert dockerignore_matches(pattern, path) is want, (
        f"the matcher got {pattern!r} vs {path!r} wrong; a matcher that "
        "matches everything would make the rule test below meaningless"
    )


@pytest.mark.parametrize("pattern,path,want", GIT_CONTROLS)
def test_the_git_matcher_can_say_no(pattern, path, want):
    assert gitignore_matches(pattern, path) is want, (
        f"the git matcher got {pattern!r} vs {path!r} wrong; a matcher that "
        "matches everything would make the rule test below meaningless"
    )


def test_the_two_matchers_disagree_about_a_bare_pattern_and_that_is_the_point():
    """The difference between git and docker IS the defect.

    A test that used one matcher for both sides could not tell the two
    worlds apart, which is how the depth hole stayed invisible: the forward
    test believed the coverage existed because it evaluated the
    .dockerignore with GIT's rules.
    """
    assert gitignore_matches("*.pkl", "sub/model.pkl") is True, (
        "git matches a bare pattern at any depth -- that is git's rule"
    )
    assert dockerignore_matches("*.pkl", "sub/model.pkl") is False, (
        "docker matches a bare pattern against the WHOLE relative path, so a "
        "bare pattern covers the ROOT ONLY"
    )
    assert dockerignore_matches("**/*.pkl", "sub/model.pkl") is True, (
        "the depth form is the one that reaches a nested file"
    )


# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------
def test_both_ignore_files_exist():
    assert GITIGNORE.is_file(), ".gitignore is missing"
    assert DOCKERIGNORE.is_file(), ".dockerignore is missing"


def test_every_gitignored_pattern_is_also_dockerignored_at_every_depth():
    """The rule, sampled at the root and at depth 1 and depth 2.

    A .gitignore pattern with no slash matches at any depth, so a bare
    .dockerignore rule leaves every nested file it names in the build
    context -- and a nested file git ignores is invisible to the deploy
    stamp while still being copied into the image.
    """
    docker_patterns = read_patterns(DOCKERIGNORE)
    assert docker_patterns, ".dockerignore has no patterns"

    uncovered = uncovered_patterns(read_patterns(GITIGNORE), docker_patterns)

    assert not uncovered, (
        "these .gitignore patterns have no covering .dockerignore pattern at "
        "this depth, so a file they match is invisible to the deploy stamp "
        "(git status does not list ignored files) yet still copied into the "
        "image by `COPY . .`: "
        + "; ".join(f"{p} at depth {d} (e.g. {s})" for p, d, s in uncovered)
    )


def test_the_depth_check_can_fail():
    """The depth check must be able to say NO.

    The check is exercised on SYNTHETIC pairs, so the repository's own
    (currently correct) data cannot satisfy it: a bare ".dockerignore" rule
    with no depth form must be reported as uncovered at depth 1 and 2.
    """
    uncovered = uncovered_patterns(["*.pkl"], ["*.pkl"])
    assert uncovered, (
        "a bare '.dockerignore' rule covers the ROOT only, so the check must "
        "report the depth samples as uncovered"
    )
    assert {depth for _pattern, depth, _sample in uncovered} == {1, 2}, (
        "the uncovered depths must be exactly 1 and 2: the root sample IS "
        "covered by the bare rule"
    )
    assert uncovered_patterns(["*.pkl"], ["*.pkl", "**/*.pkl"]) == [], (
        "adding the depth form must close the gap -- otherwise the check "
        "would report a gap that is not there"
    )
    # and the check still says NO when nothing covers the path at all
    assert uncovered_patterns(["*.pkl"], []) == [
        ("*.pkl", 0, "sample.pkl"),
        ("*.pkl", 1, "sub/sample.pkl"),
        ("*.pkl", 2, "sub/deep/sample.pkl"),
    ], (
        "with no rules at all every sample must be reported as uncovered"
    )


def _ignored_files_in_the_tree():
    """The files git ignores that are present in the tree, or None if git
    cannot answer here.

    The `safe.directory` fallback matters: the mutation harness runs pytest
    under WSL, where the Windows-mounted repository is owned by another user
    and git refuses to operate on it unless that is allowed. Without the
    fallback this test would SKIP in the harness -- fine -- but with a plain
    refusal it would also SKIP in a normal checkout, which is where the
    check is worth most.
    """
    base = ["git", "-C", str(REPO_ROOT), "ls-files", "--others", "--ignored",
            "--exclude-standard", "-z"]
    for cmd in (base, ["git", "-c", "safe.directory=*"] + base[1:]):
        out = subprocess.run(cmd, capture_output=True)
        if out.returncode == 0:
            return [f for f in out.stdout.decode("utf-8", "replace").split("\0") if f]
    return None


def test_no_gitignored_file_in_the_tree_reaches_the_image():
    """The rule against the REAL tree, with DOCKER's semantics.

    This is the check that had been passing on git's semantics: it listed
    the git-ignored files and asked a matcher that said a bare pattern
    matches at any depth, so the nested files looked covered.
    """
    docker_patterns = read_patterns(DOCKERIGNORE)
    ignored = _ignored_files_in_the_tree()
    if ignored is None:
        pytest.skip("git cannot list the ignored files here")

    leaks = [f for f in ignored if not excluded_by(docker_patterns, f)]
    assert not leaks, (
        f"{len(leaks)} git-ignored file(s) present in the tree would be copied "
        "into the image, so the running image would contain files the "
        "published archive does not: "
        + ", ".join(leaks[:10])
    )


def test_the_credential_copy_class_is_closed_on_both_sides():
    """`.env_backup` and `.env-backup` are credentials, and both sides must
    agree on them: git must ignore them AND docker must exclude them.

    The two sides are asked with their OWN matcher: git's rules for
    .gitignore and docker's rules for .dockerignore. Asking docker's
    matcher about .gitignore would be measuring git with the wrong ruler.
    """
    git_patterns = read_patterns(GITIGNORE)
    docker_patterns = read_patterns(DOCKERIGNORE)
    for sample in (".env_backup", ".env-backup", ".env.backup", "a/.env_backup"):
        assert excluded_by(docker_patterns, sample), (
            f"{sample} is a credential copy and .dockerignore does not exclude it"
        )
        assert git_excluded_by(git_patterns, sample), (
            f"{sample} is a credential copy and .gitignore does not ignore it"
        )
