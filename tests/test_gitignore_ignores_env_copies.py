# tests/test_gitignore_ignores_env_copies.py
"""No COPY of the live environment file may be committable.

THE DEFECT THIS PINS
--------------------
`.gitignore` had exactly one environment rule: `*.env`. That pattern matches a
file whose name ENDS in `.env`, and nothing else. Every OTHER name that holds
the same plaintext secrets was therefore neither ignored nor blocked, so a
plaintext copy of the live `.env` could be committed to a PUBLIC repository with
a single `git add -A`.

That is not theoretical. On 2026-11-03 three such files were found in the
production checkout, and the git status showed them as untracked (`??`), which
is exactly the state a single `git add -A` would sweep into a commit:

    ?? .env.save
    ?? .env.cr_stale_20260917T005653Z
    ?? check.txt

The first two are copies of the live environment file. The third is a scratch
file of unknown content. The hazard is the CLASS -- any name that is not
`*.env` -- so the fix is a class-wide rule, not three more names.
WHY THE TRACKED TEMPLATES MUST STAY ADDABLE
--------------------------------------------
`.env.example`, `frontend/.env.example`, `frontend/.env.production`,
`pwa/.env.example` and `pwa/.env.production` are TRACKED on purpose: they hold
placeholder or public build values, and the docker build reads
`frontend/.env.production`. A blanket `.env.*` would ignore those names, so
they are negated explicitly. A tracked file is unaffected by an ignore rule
either way, but a NEW file with one of those names would silently become
un-addable, which is the footgun this test exists to prevent.
"""

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Names that hold the live secrets, or a copy of them, and must never be
# committable. `*.env` already covers a file named exactly `.env`; these are
# the variants it missed.
MUST_BE_IGNORED = [
    ".env",
    ".env.save",
    ".env.cr_stale_20260917T005653Z",
    ".env.local",
    ".env.production.local",
    ".env.bak-20261102",
    "subdir/.env.save",
    "frontend/.env.production.local",
    "pwa/.env.local",
    "check.txt",
    # MEASURED 2026-11-03: with an UNANCHORED `!.env.production` negation these
    # three were all committable -- the negation re-opened the class at every
    # level, including the repository ROOT, where `deploy.sh:63` names
    # `.env.production` as one of the four canonical live env filenames.
    ".env.production",
    "subdir/.env.production",
    "deploy/.env.production",
    # MEASURED 2026-11-03: these match NEITHER `.env.*` NOR `*.env`, so they
    # were committable after the `.env.*` rule went in.
    ".env_backup",
    ".env_staging",
]

# Deliberately tracked: placeholder templates and the public build values the
# frontend image reads. These must stay addable.
MUST_NOT_BE_IGNORED = [
    ".env.example",
    "frontend/.env.example",
    "frontend/.env.production",
    "pwa/.env.example",
    "pwa/.env.production",
    "alembic/env.py",
    "frontend/src/vite-env.d.ts",
    "pwa/vite-env.d.ts",
]


def _is_ignored(path: str) -> bool:
    """True when the .gitignore RULES would ignore this path.

    `--no-index` is mandatory here, and it is not a stylistic choice.

    WITHOUT it, `git check-ignore` consults the index first and reports every
    TRACKED path as "not ignored" no matter what the rules say (measured 2026-11-03
    on git 2.56.0: a tracked file matching `*.log` returns exit 1 by default and
    exit 0 only with `--no-index`). Every path in MUST_NOT_BE_IGNORED is
    tracked, and every path the tracked-files test inspects is tracked too, so
    WITHOUT `--no-index` those assertions would pass unconditionally -- the
    negation rules could be deleted outright and the tests would still be
    green. The defect they exist to catch would then be entirely unguarded.

    WITH `--no-index`, git evaluates the patterns alone, which is exactly the
    question being asked: "if this path were new, would `git add` refuse it?"
    """
    p = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", "--no-index", "--", path],
        capture_output=True,
        text=True,
        encoding="utf-8", errors="replace",
    )
    # 0 = ignored, 1 = not ignored, 128 = the path is outside the repository.
    return p.returncode == 0


@pytest.mark.parametrize("path", MUST_BE_IGNORED)
def test_a_copy_of_the_live_environment_file_is_ignored(path):
    assert _is_ignored(path), (
        f"{path} is not ignored by .gitignore, so a plaintext copy of the live "
        "environment file could be committed to the PUBLIC repository"
    )


@pytest.mark.parametrize("path", MUST_NOT_BE_IGNORED)
def test_the_deliberately_tracked_templates_are_not_ignored(path):
    assert not _is_ignored(path), (
        f"{path} is deliberately tracked and must stay addable, but .gitignore "
        "now ignores it"
    )


def _tracked_files() -> list[str]:
    """The repository's tracked-file list, or a LOUD failure.

    `git ls-files` exits non-zero when git is missing, or when the path is not
    inside a repository. The return code used to be ignored, and the check
    below it read `stdout` -- so a FAILED listing produced an EMPTY list, the
    loop over that list never ran once, and "no tracked file is un-addable"
    was true because NOTHING WAS CHECKED. That is a vacuous pass, and it is
    the same failure shape as the `--no-index` defect above: the assertion
    looks meaningful and measures nothing.

    Both failure modes are now refused loudly: a non-zero exit code and an
    empty listing both raise.
    """
    p = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if p.returncode != 0:
        raise AssertionError(
            f"git ls-files failed with exit code {p.returncode}: "
            f"{p.stderr.strip()!r} -- the tracked-file check would be vacuous"
        )
    names = p.stdout.splitlines()
    if not names:
        raise AssertionError(
            "git ls-files returned no files -- the tracked-file check would "
            "be vacuous"
        )
    return names


def test_every_tracked_environment_like_file_stays_addable():
    """No file this repository tracks may become un-addable by this rule."""
    tracked = _tracked_files()

    offenders = []
    for name in tracked:
        # Only the environment-like names are in scope: the rule is about the
        # environment file and its copies, not about the whole repository.
        base = name.rsplit("/", 1)[-1]
        if not (base.startswith(".env") or base.endswith(".env")):
            continue
        if _is_ignored(name):
            offenders.append(name)

    assert not offenders, (
        "these TRACKED files are now ignored by .gitignore and would silently "
        "become un-addable: %s" % offenders
    )
