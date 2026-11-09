# tests/test_record_deployed_commit_script.py
"""The deploy stamp: which commit are we running, and are we running the
committed tree?

WHY THIS FILE EXISTS
-------------------
Two MEASURED gaps in the AGPL-3.0 section 13 source offer, both found by the
review panel on 2026-11-07, both fixed by `ops/scripts/record_deployed_commit.sh`.

1. The offer tells a reader that the archive for the running build is "the
   exact tree of the running build". The image is built from the Docker build
   CONTEXT (the Dockerfile ends with `COPY . .`), while the advertised archive
   is the COMMITTED tree, and `git pull` never removes untracked files. A
   checkout with local changes therefore produces a running image that
   contains files the published source does not. Refusing to stamp such a
   checkout makes the claim true by construction instead of leaving it to
   chance.

2. The commit has to reach the running service with no human in the loop.
   `docker-compose.yml` gives the `api` service `env_file: .env`, so a
   `DEPLOYED_COMMIT` line written into `.env` travels WITH the container and
   comes back out of `GET /api/v1/status` as the `commit` field.

WHAT IS TESTED HERE
-------------------
The REAL script, run in a throwaway git repository, under a real bash. A copy
of the logic in the test would prove nothing about the script (AGENTS.md:
"Verify against the real function, not a reimplementation").

Every behavioural assertion below was MEASURED against the real script on
LINUX, which is the platform it runs on (Elestio, CI). That matters: Git
Bash's `awk` is a MinGW build that strips a carriage return on read, so a
CRLF-preservation assertion FAILS under Git Bash and PASSES under Linux.
This file follows the repository's existing convention of driving shell code
with `bash -s` (see `tests/test_update_sh_records_the_deployed_commit.py`),
which on this machine resolves to WSL bash and in CI to the system bash.
"""

import base64
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "ops" / "scripts" / "record_deployed_commit.sh"

# The refusal message the script prints when the checkout is not the committed
# tree. It is quoted in the assertions because a refusal that does not say what
# is wrong is not an actionable refusal.
REFUSAL = "REFUSING to stamp a checkout that is not the committed tree"

# The git subcommands the stamp script is allowed to RUN. The script reads
# the repository; it never writes to it. Anything else -- `clean`, `checkout`,
# `reset`, `stash`, `rm`, `push` -- destroys work, and this script's whole
# job is to refuse rather than to "help".
ALLOWED_GIT_SUBCOMMANDS = frozenset({"rev-parse", "status"})


# --------------------------------------------------------------------------
# Static analysis helpers
# --------------------------------------------------------------------------
def _code_only(source: str) -> str:
    """The script with COMMENTS and QUOTED STRING CONTENTS removed.

    A static scan is only meaningful if it looks at what the script RUNS, not
    at what it SAYS. This script deliberately tells the operator which
    commands THEY may want to run (`git stash -u`, `git checkout -- <path>`),
    and its own header says it never runs `git clean`. A substring scan of the
    whole file therefore flags the script for containing its own warning --
    a false positive about the INSTRUMENT, not about the product. (Measured
    2026-11-09: the first version of this check did exactly that, and made
    the whole suite permanently red.)
    """
    out = []
    for raw in source.splitlines():
        kept = []
        quote = None
        i = 0
        while i < len(raw):
            ch = raw[i]
            if quote is None:
                if ch == "#":
                    break  # a comment: the rest of the line is not code
                if ch in ("'", '"'):
                    quote = ch
                    kept.append(ch)  # keep the markers, drop the content
                else:
                    kept.append(ch)
            else:
                if ch == quote:
                    quote = None
                    kept.append(ch)  # the closing marker
                # else: inside a quoted string -> the content is dropped
            i += 1
        out.append("".join(kept))
    return "\n".join(out)


def _git_subcommands(source: str) -> set:
    """Every `git <subcommand>` the script actually EXECUTES."""
    return set(re.findall(r"\bgit\s+([a-z][a-z0-9-]*)", _code_only(source)))


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _bash() -> str:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available on this machine")
    return bash


def _script_bytes() -> bytes:
    assert SCRIPT.is_file(), f"the deploy stamp script is missing: {SCRIPT}"
    return SCRIPT.read_bytes()


def _drive(setup: str = "", runs: int = 1, make_repo: bool = True) -> dict:
    """Run the REAL script in a throwaway git repository and report what happened.

    The script is fed to bash over STDIN as BYTES:
      * as a command-line argument the Windows->WSL launcher re-quotes it and
        the quoting does not survive;
      * in text mode Windows rewrites every "\\n" to "\\r\\n" on the way into
        the pipe, and bash then sees a carriage return on every line.

    Everything the script touches lives in a temp dir INSIDE the shell, so
    nothing depends on how the host translates paths. The script runs in a
    SUBSHELL: it calls `exit 1` when it refuses, and that must be the script's
    exit code, not the shell's.
    """
    body = (
        "set -u\n"
        'WORK=$(mktemp -d) || exit 1\n'
        "trap 'rm -rf \"$WORK\"' EXIT\n"
        'cd "$WORK" || exit 1\n'
    )
    if make_repo:
        body += (
            "git init -q . >/dev/null 2>&1\n"
            "git config user.email t@example.com\n"
            "git config user.name t\n"
            "printf 'print(1)\\n' > app.py\n"
            "printf 'services: {}\\n' > docker-compose.yml\n"
            # `.env` holds the live secrets and is gitignored in the real repo.
            # Without this, the temp `.env` itself would look untracked and the
            # script would refuse for the wrong reason.
            "printf '.env\\n' > .gitignore\n"
            "git add -A >/dev/null 2>&1\n"
            "git commit -qm first >/dev/null 2>&1\n"
            "SHA=$(git rev-parse HEAD)\n"
        )
    else:
        body += "SHA=none\n"
    body += setup
    # The script, in a subshell, `runs` times.
    for _ in range(runs):
        body += "(\n" + _script_bytes().decode("utf-8") + "\n)\n"
    body += (
        "rc=$?\n"
        "printf 'RC=%s\\n' \"$rc\"\n"
        "if [ -f \"$WORK/.env\" ]; then\n"
        "  printf 'ENV=%s\\n' \"$(base64 \"$WORK/.env\" | tr -d '\\n')\"\n"
        "else\n"
        "  printf 'ENV=MISSING\\n'\n"
        "fi\n"
        "printf 'FILES=%s\\n' \"$(ls -1A \"$WORK\" | tr '\\n' ',')\"\n"
        "printf 'SUBFILES=%s\\n' \"$(ls -1A \"$WORK/sub\" 2>/dev/null | tr '\\n' ',')\"\n"
        "printf 'SHA=%s\\n' \"$SHA\"\n"
    )

    proc = subprocess.run(
        [_bash(), "-s"],
        input=body.encode("utf-8"),
        capture_output=True,
        timeout=300,
    )
    out = proc.stdout.decode("utf-8", errors="replace")
    err = proc.stderr.decode("utf-8", errors="replace")
    fields = {}
    for line in out.splitlines():
        key, _sep, value = line.partition("=")
        if key in ("RC", "ENV", "FILES", "SUBFILES", "SHA"):
            fields[key] = value
    env_b64 = fields.get("ENV", "MISSING")
    return {
        "rc": int(fields.get("RC", "255")),
        "stdout": out,
        "stderr": err,
        "env": b"" if env_b64 == "MISSING" else base64.b64decode(env_b64),
        "env_exists": env_b64 != "MISSING",
        "files": fields.get("FILES", ""),
        "subfiles": fields.get("SUBFILES", ""),
        "sha": fields.get("SHA", ""),
    }


# --------------------------------------------------------------------------
# The script records the commit it is about to build
# --------------------------------------------------------------------------
class TestTheStampRecordsTheCommit:
    def test_a_clean_checkout_records_the_commit(self):
        result = _drive("printf 'POSTGRES_PASSWORD=secret\\n' > .env\n")
        assert result["rc"] == 0, result["stderr"]
        assert f"DEPLOYED_COMMIT={result['sha']}\n".encode() in result["env"]

    def test_the_value_is_the_head_commit_and_not_something_invented(self):
        """The recorded value must be `git rev-parse HEAD`, verbatim."""
        result = _drive("printf 'POSTGRES_PASSWORD=secret\\n' > .env\n")
        assert result["sha"] in result["stdout"]
        assert f"DEPLOYED_COMMIT={result['sha']}".encode() in result["env"]

    def test_the_other_lines_of_the_environment_file_are_untouched(self):
        """The operator's file holds the live secrets. It must survive intact."""
        result = _drive(
            "printf 'POSTGRES_PASSWORD=secret\\nREDIS_API_PASSWORD=other\\n' > .env\n"
        )
        assert result["rc"] == 0, result["stderr"]
        assert b"POSTGRES_PASSWORD=secret\n" in result["env"]
        assert b"REDIS_API_PASSWORD=other\n" in result["env"]

    def test_recording_twice_leaves_exactly_one_commit_line(self):
        """The script runs on every deploy; it must be idempotent."""
        result = _drive("printf 'POSTGRES_PASSWORD=secret\\n' > .env\n", runs=2)
        assert result["rc"] == 0, result["stderr"]
        assert result["env"].count(b"DEPLOYED_COMMIT=") == 1

    def test_an_existing_value_is_replaced_not_appended(self):
        """Two DEPLOYED_COMMIT lines would make the published commit ambiguous."""
        result = _drive(
            "printf 'DEPLOYED_COMMIT=0000000000000000000000000000000000000000\\n' "
            "> .env\n"
        )
        assert result["rc"] == 0, result["stderr"]
        assert result["env"].count(b"DEPLOYED_COMMIT=") == 1
        assert b"DEPLOYED_COMMIT=0000000000000000000000000000000000000000" not in (
            result["env"]
        )

    def test_a_crlf_environment_file_keeps_its_carriage_returns(self):
        """A Windows-edited .env must not have its endings rewritten."""
        result = _drive(
            "printf 'POSTGRES_PASSWORD=secret\\r\\nDEPLOYED_COMMIT=old\\r\\n' > .env\n"
        )
        assert result["rc"] == 0, result["stderr"]
        assert b"POSTGRES_PASSWORD=secret\r\n" in result["env"]
        assert f"DEPLOYED_COMMIT={result['sha']}\r\n".encode() in result["env"]
        assert b"DEPLOYED_COMMIT=old" not in result["env"]

    def test_an_lf_environment_file_does_not_gain_a_carriage_return(self):
        result = _drive("printf 'POSTGRES_PASSWORD=secret\\n' > .env\n")
        assert result["rc"] == 0, result["stderr"]
        assert b"\r" not in result["env"]


# --------------------------------------------------------------------------
# A checkout that is not the committed tree is refused, and nothing is lost
# --------------------------------------------------------------------------
class TestADirtyCheckoutIsRefused:
    def test_an_untracked_file_is_refused(self):
        result = _drive(
            "printf 'POSTGRES_PASSWORD=secret\\n' > .env\n"
            "printf 'SECRET = 1\\n' > sneaky.py\n"
        )
        assert result["rc"] == 1, (result["stdout"], result["stderr"])
        assert REFUSAL in result["stderr"]
        assert "sneaky.py" in result["stderr"]

    def test_the_refusal_changes_nothing_in_the_environment_file(self):
        result = _drive(
            "printf 'POSTGRES_PASSWORD=secret\\n' > .env\n"
            "printf 'SECRET = 1\\n' > sneaky.py\n"
        )
        assert result["rc"] == 1
        assert result["env"] == b"POSTGRES_PASSWORD=secret\n", (
            "the refusal rewrote the environment file"
        )

    def test_the_refusal_deletes_nothing(self):
        """A dirty deployment is a human problem. This script must not solve it
        by throwing the work away."""
        result = _drive(
            "printf 'POSTGRES_PASSWORD=secret\\n' > .env\n"
            "printf 'SECRET = 1\\n' > sneaky.py\n"
        )
        assert result["rc"] == 1
        assert "sneaky.py" in result["files"]

    def test_a_modified_tracked_file_is_refused(self):
        result = _drive(
            "printf 'POSTGRES_PASSWORD=secret\\n' > .env\n"
            "printf 'print(2)\\n' > app.py\n"
        )
        assert result["rc"] == 1, (result["stdout"], result["stderr"])
        assert REFUSAL in result["stderr"]
        assert "app.py" in result["stderr"]

    def test_the_refusal_does_not_discard_the_modification(self):
        result = _drive(
            "printf 'POSTGRES_PASSWORD=secret\\n' > .env\n"
            "printf 'print(2)\\n' > app.py\n"
        )
        assert result["rc"] == 1
        assert "app.py" in result["files"]

    def test_the_script_contains_no_command_that_could_destroy_work(self):
        """The refusal says what is wrong and stops. It must never `clean`,
        `checkout`, `reset` or `stash` the tree.

        Checked as an ALLOWLIST of the git subcommands the script actually
        RUNS, not as a blacklist of strings appearing somewhere in the file.
        The refusal deliberately names `git stash -u` and `git checkout -- <path>`
        as suggestions for the OPERATOR, and the header says the script never
        runs `git clean` -- so a substring scan flags the script for containing
        its own warning. The allowlist is STRICTER than the blacklist it
        replaces: it also catches `git rm`, `git push` or any other
        subcommand nobody thought to blacklist.
        """
        subcommands = _git_subcommands(SCRIPT.read_text(encoding="utf-8"))
        assert subcommands <= ALLOWED_GIT_SUBCOMMANDS, (
            f"the stamp script runs git subcommand(s) it must never run: "
            f"{sorted(subcommands - ALLOWED_GIT_SUBCOMMANDS)}"
        )


# --------------------------------------------------------------------------
# The scanner above is an INSTRUMENT. An instrument that cannot fail is not
# an instrument, so the scanner has its own controls.
# --------------------------------------------------------------------------
class TestTheStaticScannerItself:
    def test_it_flags_a_script_that_really_does_reset_the_tree(self):
        assert "reset" in _git_subcommands(
            "#!/bin/bash\nset -euo pipefail\ngit reset --hard HEAD\n"
        )

    def test_it_flags_a_script_that_really_does_clean_the_tree(self):
        assert "clean" in _git_subcommands("#!/bin/bash\ngit clean -fdx\n")

    def test_it_flags_a_script_that_really_does_stash_the_tree(self):
        assert "stash" in _git_subcommands("#!/bin/bash\ngit stash -u\n")

    def test_it_flags_a_script_that_really_does_check_out_the_tree(self):
        assert "checkout" in _git_subcommands("#!/bin/bash\ngit checkout -- .\n")

    def test_it_flags_a_destructive_command_hidden_in_a_substitution(self):
        assert "reset" in _git_subcommands(
            "#!/bin/bash\nX=$(git rev-parse HEAD)\ngit reset --hard\n"
        )

    def test_it_ignores_a_command_the_script_only_mentions_in_a_comment(self):
        assert _git_subcommands(
            "#!/bin/bash\n# this script never runs git reset --hard\ngit rev-parse HEAD\n"
        ) == {"rev-parse"}

    def test_it_ignores_a_command_the_script_only_says_out_loud(self):
        assert _git_subcommands(
            '#!/bin/bash\necho "  git stash -u   # park the changes" >&2\n'
        ) == set()

    def test_a_comment_cannot_smuggle_a_command_past_it(self):
        assert "clean" not in _git_subcommands(
            "#!/bin/bash\ngit status --porcelain # git clean -fdx\n"
        )

    def test_it_accepts_the_real_script(self):
        subcommands = _git_subcommands(SCRIPT.read_text(encoding="utf-8"))
        assert subcommands <= ALLOWED_GIT_SUBCOMMANDS
        assert subcommands, "the scanner found no git call at all -- it is not working"


# --------------------------------------------------------------------------
# The refusal is loud, and the file it needs must already exist
# --------------------------------------------------------------------------
class TestTheRefusalIsLoud:
    def test_a_missing_environment_file_is_refused(self):
        """The .env holds the live secrets and is gitignored, so it is the
        operator's file. Creating a stub would start the application with no
        configuration at all -- a silent outage -- so the script stops."""
        result = _drive("")
        assert result["rc"] == 1, (result["stdout"], result["stderr"])
        assert ".env" in result["stderr"]

    def test_a_missing_environment_file_is_not_created(self):
        result = _drive("")
        assert result["rc"] == 1
        assert not result["env_exists"], (
            "the script created a stub .env; that would start the app with no "
            "configuration"
        )

    def test_outside_a_git_repository_the_script_refuses(self):
        result = _drive("", make_repo=False)
        assert result["rc"] == 1, (result["stdout"], result["stderr"])
        assert "git" in result["stderr"].lower()

    def test_the_refusal_explains_itself(self):
        """A refusal that does not say what is wrong cannot be acted on."""
        result = _drive("printf 'SECRET = 1\\n' > sneaky.py\n")
        assert result["rc"] == 1
        assert result["stderr"].strip(), "the refusal printed nothing"
        assert "git status --porcelain" in result["stderr"], (
            "the refusal must show the operator the same list it saw"
        )


# --------------------------------------------------------------------------
# The script finds the project root, so it can be run from anywhere
# --------------------------------------------------------------------------
class TestTheScriptFindsTheProjectRoot:
    def test_run_from_a_subdirectory_it_still_stamps_the_root(self):
        """The deploy path in AGENTS.md runs from the project directory, but a
        script that only works from one place is a trap for the next person."""
        result = _drive(
            "mkdir -p sub\n"
            "printf 'POSTGRES_PASSWORD=secret\\n' > .env\n"
            "cd sub\n"
        )
        assert result["rc"] == 0, result["stderr"]
        assert f"DEPLOYED_COMMIT={result['sha']}\n".encode() in result["env"]
        assert ".env" not in result["subfiles"], (
            "the script wrote the environment file into the subdirectory"
        )


# --------------------------------------------------------------------------
# The script is safe to RUN
# --------------------------------------------------------------------------
class TestTheScriptIsSafeToRun:
    def test_the_script_exists_and_is_a_shell_script(self):
        assert SCRIPT.is_file(), f"{SCRIPT} must exist"
        assert SCRIPT.read_text(encoding="utf-8").startswith("#!/bin/bash")

    def test_the_script_fails_closed(self):
        """A stamp that failed quietly would leave a WRONG commit published."""
        source = SCRIPT.read_text(encoding="utf-8")
        assert "set -euo pipefail" in source, (
            "the script must fail closed: `set -euo pipefail`"
        )

    def test_the_refusal_comes_before_the_write(self):
        """The order matters: the refusal has to come BEFORE the write, or a
        dirty checkout would already have been stamped."""
        source = SCRIPT.read_text(encoding="utf-8")
        refuse_at = source.index("REFUSING to stamp a checkout")
        write_at = source.index('DEPLOYED_COMMIT=" sha cr')
        assert refuse_at < write_at, (
            "the script writes the commit before it refuses a dirty checkout"
        )
