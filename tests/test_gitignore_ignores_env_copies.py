# tests/test_gitignore_ignores_env_copies.py
"""No copy of the live environment file that KEEPS the `.env` prefix may be
committable.

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

WHAT THIS TEST DOES NOT CLAIM
-----------------------------
A copy RENAMED so that it no longer STARTS with `.env` -- `prod.env.bak`,
`copy-of-env.txt` -- cannot be caught by ANY name-based rule, and this test
does not pretend otherwise. The guarantee is about the class of copies that
keep the `.env` prefix with one of the three separators the rules cover (DOT,
UNDERSCORE, HYPHEN). An earlier version of this file claimed it closed "the
whole class" while `.env-backup`, `.env-copy` and `.env-20261103` were still
committable; that overclaim is what the 2026-11-04 review panel caught.

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
    # MEASURED 2026-11-04 BY THE REVIEW PANEL: the HYPHEN separator was STILL
    # a hole. `.env-backup`, `.env-copy` and `.env-20261103` matched NEITHER
    # `.env.*` NOR `.env_*` NOR `*.env`, so `git add -A` still swept them into
    # a PUBLIC commit. A date-stamped backup is exactly the shape a real
    # credential copy takes -- the production checkout already held one
    # (`.env.cr_stale_20260917T005653Z`).
    ".env-backup",
    ".env-copy",
    ".env-20261103",
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


def _is_in_scope(basename: str) -> bool:
    """True when the TRACKED-FILE guard must inspect this basename.

    The guard below can only protect a name it RECOGNISES. It has to recognise
    every basename this diff newly ignores -- and `check.txt` matches NEITHER
    `startswith(".env")` NOR `endswith(".env")`. Leaving it out (as the first
    version of this file did) meant a tracked `check.txt` would have become
    un-addable with NOTHING guarding it, while the guard still reported green.

    This is a named function rather than an inline condition on purpose: it
    makes the coverage itself testable, so removing a clause from it is a
    change the suite CATCHES instead of one it silently absorbs.
    """
    return (
        basename.startswith(".env")
        or basename.endswith(".env")
        or basename == "check.txt"
    )


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


@pytest.mark.parametrize(
    "basename",
    [
        ".env",
        ".env.save",
        ".env_backup",
        ".env-backup",
        ".env-20261103",
        ".env.example",
        "check.txt",
    ],
)
def test_the_tracked_file_guard_inspects_every_name_this_diff_ignores(basename):
    """The guard must RECOGNISE every name this diff newly ignores.

    A tracked file that this diff newly ignores would become silently
    un-addable. The guard that prevents that can only see the names it
    recognises, so the coverage of the guard is itself asserted here.
    """
    assert _is_in_scope(basename), (
        f"{basename} is newly ignored by .gitignore but the tracked-file guard "
        "does not recognise it, so a tracked file of that name would become "
        "un-addable with nothing guarding it"
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
    inspected = 0
    for name in tracked:
        # Only the names this diff newly ignores are in scope: the rule is
        # about the environment file, its copies and the one scratch name --
        # not about the whole repository.
        base = name.rsplit("/", 1)[-1]
        if not _is_in_scope(base):
            continue
        inspected += 1
        if _is_ignored(name):
            offenders.append(name)

    assert inspected > 0, (
        "the tracked-file scan inspected NOTHING -- the check would be vacuous, "
        "which is exactly the failure it was written to prevent"
    )

    assert not offenders, (
        "these TRACKED files are now ignored by .gitignore and would silently "
        "become un-addable: %s" % offenders
    )


def test_the_probe_is_only_meaningful_with_no_index():
    """PROVE the `--no-index` requirement instead of asserting it.

    WITHOUT `--no-index`, `git check-ignore` consults the index first and
    reports every TRACKED path as "not ignored" no matter what the rules say.
    The whole suite would then pass while measuring nothing -- which is the
    failure this file was written to prevent, so the claim deserves a
    mechanical proof rather than a comment.

    This builds a throwaway repository with a TRACKED file that an ignore
    rule matches, and shows the two answers genuinely differ. It touches
    nothing in this repository.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / ".gitignore").write_text("*.env\n", encoding="utf-8")
        (repo / "tracked.env").write_text("placeholder\n", encoding="utf-8")

        run = subprocess.run(
            ["git", "init", "-q", str(repo)], capture_output=True, text=True
        )
        assert run.returncode == 0, f"git init failed: {run.stderr!r}"

        run = subprocess.run(
            # `-f` is REQUIRED, and it is the whole point of the control: the
            # file matches the ignore rule, so a plain `git add` REFUSES it.
            # A file that is TRACKED *despite* matching a rule is exactly the
            # case where check-ignore answers differently with and without
            # `--no-index`.
            ["git", "-C", str(repo), "add", "-f", "tracked.env"],
            capture_output=True, text=True,
        )
        assert run.returncode == 0, f"git add failed: {run.stderr!r}"

        # WITHOUT --no-index: git consults the index, so the tracked file is
        # reported as NOT ignored even though the rule matches it.
        with_index = subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "-q", "--", "tracked.env"],
            capture_output=True,
        )
        # WITH --no-index: the rules alone are evaluated, so it IS ignored.
        without_index = subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "-q", "--no-index", "--", "tracked.env"],
            capture_output=True,
        )

        assert with_index.returncode == 1, (
            "a TRACKED file matching an ignore rule must be reported as NOT "
            "ignored WITHOUT --no-index; if git says otherwise this control is "
            "not testing what it claims"
        )
        assert without_index.returncode == 0, (
            "the same TRACKED file must be reported as IGNORED WITH --no-index"
        )
