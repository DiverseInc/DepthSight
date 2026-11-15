"""update.sh may READ the stamped commit; it must never WRITE it.

WHY THIS FILE EXISTS
The existing guard in tests/test_update_sh_records_the_deployed_commit.py
asserts that the literal string "DEPLOYED_COMMIT=" does not appear in
update.sh. That guard can be evaded: naming the key in a variable
(stamp_key="DEPLOYED_COMMIT") keeps the literal out of the file while
still allowing `echo "${stamp_key}=..." > .env`. The guard was written to
protect one invariant -- the stamping logic lives in exactly one place --
so this file pins THAT invariant instead of the spelling of it.

A note on the scan: a redirection inside a comment does not write anything,
and `2>/dev/null` / `2>&1` are error redirections, not writes to the
environment file. Both are excluded, and the exclusion is written so that a
comment cannot hide a real write.
"""

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STAMP_SCRIPT = "ops/scripts/record_deployed_commit.sh"

SKIP_DIRS = {
    ".git", ".github", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", "data", "data_storage", "logs", ".review", ".agent",
    ".scratch", "scratch", "md", ".smart-env", "dist", "build",
}


def _shell_scripts():
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.endswith(".sh"):
                yield Path(dirpath) / name


def _executable_lines(path: Path):
    """The lines of a shell script that are not comments and not blank."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(stripped)
    return out


# A redirection that is NOT an error redirection: `2>/dev/null`, `2>&1` and
# `&>` are excluded. The lookbehind rejects a `>` preceded by a digit or `&`.
REDIRECT_RE = re.compile(r"(?<![\d&])>{1,2}(?!&)\s*[^|\n]*")
ENV_TARGET_RE = re.compile(r"\.env\b|ENV_FILE", re.IGNORECASE)


class TestOnlyTheStampScriptWritesTheStamp:
    def test_update_sh_never_redirects_output_into_the_environment_file(self):
        """A write to .env is how a stamp would be planted."""
        script = REPO_ROOT / "update.sh"
        offenders = []
        for line in _executable_lines(script):
            for m in REDIRECT_RE.finditer(line):
                if ENV_TARGET_RE.search(m.group(0)):
                    offenders.append(line)
        assert not offenders, (
            "update.sh redirects output into the environment file: "
            f"{offenders}. update.sh must READ the stamp, never write it."
        )

    def test_update_sh_never_assigns_the_stamp_key(self):
        """`DEPLOYED_COMMIT=<value>` in update.sh would be a second stamp."""
        script = REPO_ROOT / "update.sh"
        offenders = [
            line for line in _executable_lines(script)
            if re.search(r"DEPLOYED_COMMIT\s*=", line)
        ]
        assert not offenders, (
            f"update.sh assigns the stamp key itself: {offenders}. The stamping "
            f"logic must live only in {STAMP_SCRIPT}."
        )

    def test_no_other_script_writes_the_stamp(self):
        """Only the shared stamp script may write DEPLOYED_COMMIT.

        This is the invariant the literal-string guard was aiming at, checked
        across EVERY shell script in the repository, so it cannot be evaded by
        moving the key into a variable in one file.
        """
        writers = []
        for path in _shell_scripts():
            rel = path.relative_to(REPO_ROOT).as_posix()
            for line in _executable_lines(path):
                if re.search(r"DEPLOYED_COMMIT\s*=", line):
                    writers.append((rel, line))
        offenders = [w for w in writers if w[0] != STAMP_SCRIPT]
        assert not offenders, (
            f"scripts other than {STAMP_SCRIPT} write the stamp: {offenders}. "
            "The stamping logic must live in exactly one place."
        )

    def test_the_stamp_script_is_the_one_that_writes_it(self):
        """Guard against the check above passing because nothing writes it."""
        script = REPO_ROOT / STAMP_SCRIPT
        assert script.exists(), f"{STAMP_SCRIPT} is missing"
        writers = [
            line for line in _executable_lines(script)
            if re.search(r"DEPLOYED_COMMIT\s*=", line)
        ]
        assert writers, (
            f"{STAMP_SCRIPT} no longer assigns DEPLOYED_COMMIT; the stamp would "
            "never be written and the offer would have nothing to offer."
        )
