# File: tests/test_deploy_script_clones_this_fork.py
"""
`deploy.sh` cloned the UPSTREAM repository, not this fork.

Measured 2026-11-05 on `origin/main` (b64e7c6):

    deploy.sh:97:  git clone https://github.com/DepthSight-Pro/DepthSight.git "$PROJECT_DIR"

The script is inherited from upstream, so the URL it clones points at upstream.
The local-files branch (deploy.sh:89) is why production is unaffected today --
a box that already carries the project files skips the clone -- so the defect
is LATENT: a FRESH deployment onto a machine that has no project files yet
(the "deploy the product to a new machine" path) silently installs UPSTREAM's
code, with upstream's branding and upstream's pricing, and then generates our
.env on top of it.

A deploy that installs the wrong product is worse than a deploy that stops
with a clear message, so the script now:

  * clones THIS fork (``DiverseInc/DepthSight``),
  * refuses to continue when the clone fails,
  * verifies the checkout really is this fork before .env is generated on top
    of it (a box whose /opt/depthsight already holds another repository is
    caught instead of being deployed silently),
  * prints the repository URL it is deploying from.

This file is the tripwire. It reads ONE file and asserts on its text: no
network, no docker, no environment, no fixtures.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = REPO_ROOT / "deploy.sh"

# This fork. The clone URL was taken from `git remote -v` in the repo:
#   origin  https://github.com/DiverseInc/DepthSight.git (fetch/push)
FORK_SLUG = "DiverseInc/DepthSight"
FORK_URL = "https://github.com/DiverseInc/DepthSight.git"

# The repository the script used to clone. Upstream is NOT this fork.
UPSTREAM_CLONE_URL = "https://github.com/DepthSight-Pro/DepthSight.git"

# `git clone` and its arguments, up to the end of the line.
CLONE_RE = re.compile(r"\bgit\s+clone\b(?P<args>[^\n]*)")

# A `git clone` is only a clone COMMAND when it sits at a command position.
# The same two words inside an echo message ("FATAL: git clone of ...") are
# prose, and reading them as a command once made this test fail against a
# CORRECT deploy.sh.
COMMAND_ENDINGS = (";", "&", "|", "(", "!", "{", "}", "then", "do", "else", "if")

# `NAME="value"` assignments, so a clone target given as "$NAME" can be
# resolved to the value the script actually uses.
ASSIGNMENT_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)=[\"']?(?P<value>[^\"'\n]+)[\"']?\s*$",
    re.MULTILINE,
)


def _script_text() -> str:
    return DEPLOY_SCRIPT.read_text(encoding="utf-8")


def _strip_comment_lines(text: str) -> str:
    """Drop whole-line comments so a comment cannot read as a clone target."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _repo_slug(url: str) -> str:
    """Normalise a git URL to "owner/repo".

    Mirrors the script's own `repo_slug` helper: the checks compare
    REPOSITORIES, not URL spelling (https/ssh/git@, with or without ".git").
    """
    slug = url.strip()
    if slug.endswith(".git"):
        slug = slug[: -len(".git")]
    for prefix in (
        "ssh://git@github.com/",
        "git@github.com:",
        "https://github.com/",
        "http://github.com/",
    ):
        if slug.startswith(prefix):
            slug = slug[len(prefix) :]
            break
    return slug.rstrip("/")


def _is_clone_command(line: str, match_start: int) -> bool:
    """True when `git clone` is a command, not part of a quoted message."""
    prefix = line[:match_start]
    if prefix.count('"') % 2 or prefix.count("'") % 2:
        return False  # inside a quoted string
    stripped = prefix.rstrip()
    if not stripped:
        return True  # first word on the line
    return stripped.endswith(COMMAND_ENDINGS)


def _clone_targets(text: str) -> list[str]:
    """Every repository the script could clone from, with variables resolved.

    A clone target may be a literal URL or a shell variable, so a change to
    the variable's VALUE is caught as well as a change to the clone line.
    """
    assignments = {
        match.group("name"): match.group("value").strip()
        for match in ASSIGNMENT_RE.finditer(text)
    }

    targets: list[str] = []
    for line in _strip_comment_lines(text).splitlines():
        for match in CLONE_RE.finditer(line):
            if not _is_clone_command(line, match.start()):
                continue
            for token in match.group("args").split():
                if token.startswith("-"):
                    continue  # `git clone --depth 1 <url> <dir>`
                token = token.strip("\"'")
                if token.startswith("${") and token.endswith("}"):
                    name = token[2:-1]
                elif token.startswith("$"):
                    name = token[1:]
                else:
                    name = None
                targets.append(assignments.get(name, "") if name else token)
                break  # the first non-flag argument is the repository
    return targets


def _project_setup_block(text: str) -> str:
    """The "3. Project Setup" section -- the part this fix owns."""
    start = text.index("# 3. Project Setup")
    end = text.index("# 4. Environment Configuration")
    return text[start:end]


def test_deploy_script_clones_this_fork():
    """Every clone target in the script must be THIS fork."""
    text = _script_text()
    targets = _clone_targets(text)

    assert targets, "deploy.sh no longer contains a `git clone` invocation"

    for target in targets:
        assert _repo_slug(target) == FORK_SLUG, (
            f"deploy.sh clones {target!r}, which is not this fork "
            f"({FORK_SLUG}). A fresh deployment would install the wrong "
            f"product -- upstream's code and branding -- and then generate "
            f"our .env on top of it."
        )


def test_deploy_script_never_contains_the_upstream_clone_url():
    """The upstream clone URL must not be anywhere in the script."""
    text = _script_text()
    assert UPSTREAM_CLONE_URL not in text, (
        f"deploy.sh still contains the upstream clone URL "
        f"({UPSTREAM_CLONE_URL}). A fresh deployment would install upstream's "
        f"product instead of this fork's."
    )


def test_deploy_script_states_the_fork_url():
    """The script names this fork's URL, so the operator can see it."""
    assert FORK_URL in _script_text()


def test_deploy_script_fails_loudly_instead_of_deploying_the_wrong_product():
    """A refused deploy must be loud, and the checkout must be verified."""
    block = _project_setup_block(_script_text())

    assert "git clone" in block

    # A failed clone must stop the deploy, not fall through to .env generation.
    assert not re.search(r"git\s+clone[^\n]*\|\|\s*true", block), (
        "deploy.sh swallows a failed clone. A deploy that installs the wrong "
        "product is worse than a deploy that stops with a clear message."
    )
    assert re.search(r"exit\s+1", block), (
        "the project-setup block no longer exits non-zero when the clone or "
        "the checkout verification fails."
    )

    # The checkout is verified before .env is generated on top of it.
    assert "remote.origin.url" in block, (
        "deploy.sh no longer verifies that the checkout is this fork."
    )

    # The operator is told which repository is being deployed.
    assert re.search(r"echo[^\n]*(DEPLOY_REPO_URL|ACTUAL_REPO_URL)", block), (
        "deploy.sh no longer prints the repository URL it is deploying from."
    )


def test_local_project_files_path_is_preserved():
    """Production depends on this branch; it must skip the clone as before."""
    text = _script_text()
    assert 'if [ -f "deploy.sh" ] && [ -d "api" ]; then' in text
    assert "Detected local project files" in text
