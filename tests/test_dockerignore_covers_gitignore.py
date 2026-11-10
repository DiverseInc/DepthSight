# tests/test_dockerignore_covers_gitignore.py
"""Nothing that .gitignore excludes may enter the built image.

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

The hole was measured, not assumed:

  * `.gitignore` closes the credential-copy class with FOUR patterns --
    `.env`, `.env.*`, `.env_*` and `.env-*` (dot, underscore and hyphen
    separators). `.dockerignore` had only `.env` and `.env.*`, so a
    `.env_backup` or a `.env-backup` was gitignored yet still visible to
    the build context.
  * 30 of the 46 positive `.gitignore` patterns had no covering
    `.dockerignore` pattern.
  * 48 of the 451 git-ignored files present in the tree would have been
    copied into the image -- including the review panel's own transcripts
    and the full `review.diff`.

THE RULE
--------
Every pattern `.gitignore` excludes must also be excluded by
`.dockerignore`. That is the invariant that makes the offer's promise true
by construction rather than by luck.

THE OTHER DIRECTION
-------------------
That rule is ONE direction. The reverse -- "does a `.dockerignore` pattern
exclude a file that git TRACKS?" -- is a different question, and it was not
checked here: a tracked file excluded from the build context does not make
the checkout dirty, so the deploy stamp still calls it the committed tree
while the image is built without it. That reverse direction, and the
explicit allowlist of the tracked files that may be excluded, are pinned in
`tests/test_dockerignore_tracked_files.py`.

THE MATCHER
-----------
Docker's own pattern matcher is not callable from a test without a docker
daemon, so this file implements the subset of the syntax this repository
uses -- plain names, a trailing `/` (a directory), a leading `/` (anchored
to the context root), `*` (not crossing `/`), `**` (crossing `/`) and `?`.
A reimplementation is only acceptable if it can FAIL, so the matcher has
its own controls: each one asserts that a pattern does NOT match a path it
must not match. A matcher that matches everything would pass the rule test
and is caught by the controls.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GITIGNORE = REPO_ROOT / ".gitignore"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"


# --------------------------------------------------------------------------
# The matcher
# --------------------------------------------------------------------------
def dockerignore_matches(pattern: str, path: str) -> bool:
    """Does a .dockerignore pattern exclude this path?

    The subset of Docker's syntax this repository uses. Docker disregards
    leading and trailing slashes; a pattern containing a `/` is anchored to
    the context root, one without a `/` matches at any level.
    """
    p = pattern.strip()
    if not p or p.startswith("#"):
        return False
    anchored = "/" in p.rstrip("/")
    p = p.strip("/")
    if not p:
        return False

    rx = []
    i = 0
    while i < len(p):
        if p.startswith("**", i):
            rx.append(".*")
            i += 2
            continue
        c = p[i]
        if c == "*":
            rx.append("[^/]*")
        elif c == "?":
            rx.append("[^/]")
        else:
            rx.append(re.escape(c))
        i += 1
    body = "".join(rx)

    if anchored:
        return re.fullmatch(body + r"(?:/.*)?", path) is not None
    # Unanchored: it may match any single segment, and everything below it.
    for seg in path.split("/"):
        if re.fullmatch(body, seg) is not None:
            return True
    return re.fullmatch(body + r"(?:/.*)?", path) is not None


def read_patterns(path: Path) -> list:
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def sample_path_for(pattern: str) -> str:
    """A concrete path the gitignore pattern would match."""
    p = pattern.rstrip("/")
    if p.startswith("/"):
        p = p[1:]
    if p.startswith("**/"):
        p = p[3:]
    p = p.replace("**/", "")
    if p.endswith("/"):
        return p + "x"
    s = re.sub(r"\*\*", "deep", p)
    s = re.sub(r"\*", "sample", s)
    s = re.sub(r"\?", "x", s)
    return s + ("x" if s.endswith("/") else "")


# --------------------------------------------------------------------------
# The matcher's own controls: a matcher that cannot say NO is not a matcher
# --------------------------------------------------------------------------
CONTROLS = [
    (".env", ".env", True),
    (".env", ".envx", False),
    (".env", "a/.env", True),
    (".env.*", ".env.backup", True),
    (".env.*", ".env_backup", False),
    (".env.*", ".env-backup", False),
    ("data/", "data/x", True),
    ("data/", "database/x", False),
    ("data/", "a/data/x", True),
    (".review/", ".review/x", True),
    (".review/", "x/.review/y", True),
    ("**/.env", "a/b/.env", True),
    ("pwa/node_modules/", "pwa/node_modules/x", True),
    ("pwa/node_modules/", "frontend/node_modules/x", False),
    ("*.log", "a/b.log", True),
    ("*.log", "a/b.logs", False),
]


@pytest.mark.parametrize("pattern,path,want", CONTROLS)
def test_the_matcher_can_say_no(pattern, path, want):
    assert dockerignore_matches(pattern, path) is want, (
        f"the matcher got {pattern!r} vs {path!r} wrong; a matcher that matches "
        "everything would make the rule test below meaningless"
    )


# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------
def test_both_ignore_files_exist():
    assert GITIGNORE.is_file(), ".gitignore is missing"
    assert DOCKERIGNORE.is_file(), ".dockerignore is missing"


def test_every_gitignored_pattern_is_also_dockerignored():
    docker_patterns = read_patterns(DOCKERIGNORE)
    assert docker_patterns, ".dockerignore has no patterns"

    uncovered = []
    for pattern in read_patterns(GITIGNORE):
        if pattern.startswith("!"):
            continue  # a negation UN-ignores: that path is tracked, so it
            # belongs in the image with the rest of the committed tree.
        sample = sample_path_for(pattern)
        if not sample:
            continue
        if not any(dockerignore_matches(d, sample) for d in docker_patterns):
            uncovered.append((pattern, sample))

    assert not uncovered, (
        "these .gitignore patterns have no covering .dockerignore pattern, so "
        "a file they match is invisible to the deploy stamp (git status does "
        "not list ignored files) yet still copied into the image by "
        "`COPY . .`: "
        + "; ".join(f"{p} (e.g. {s})" for p, s in uncovered)
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
    docker_patterns = read_patterns(DOCKERIGNORE)
    ignored = _ignored_files_in_the_tree()
    if ignored is None:
        pytest.skip("git cannot list the ignored files here")

    leaks = [f for f in ignored
             if not any(dockerignore_matches(d, f) for d in docker_patterns)]
    assert not leaks, (
        f"{len(leaks)} git-ignored file(s) present in the tree would be copied "
        "into the image, so the running image would contain files the "
        "published archive does not: "
        + ", ".join(leaks[:10])
    )


def test_the_credential_copy_class_is_closed_on_both_sides():
    """`.env_backup` and `.env-backup` are credentials, and both sides must
    agree on them: git must ignore them AND docker must exclude them."""
    git_patterns = read_patterns(GITIGNORE)
    docker_patterns = read_patterns(DOCKERIGNORE)
    for sample in (".env_backup", ".env-backup", ".env.backup", "a/.env_backup"):
        assert any(dockerignore_matches(d, sample) for d in docker_patterns), (
            f"{sample} is a credential copy and .dockerignore does not exclude it"
        )
        assert any(dockerignore_matches(g, sample) for g in git_patterns), (
            f"{sample} is a credential copy and .gitignore does not ignore it"
        )
