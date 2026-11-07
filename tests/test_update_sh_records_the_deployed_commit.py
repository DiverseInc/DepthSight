# tests/test_update_sh_records_the_deployed_commit.py
"""The deploy script must publish the commit it built, and must not call a
broken deploy a success.

THE DEFECT THIS PINS
--------------------
Two MEASURED gaps in `update.sh`, both found by the review panel on
2026-11-07 and both fixed here.

1. (Copilot, panel #3, condition (a)) The AGPL-3.0 section 13 offer pointed
   the reader at the tip of `main` and told them to ASK BY EMAIL for the
   exact commit behind the running build. There was no machine-checkable link
   between the running build and its source, so the email request was
   load-bearing for the one thing section 13 asks for: the source of the
   version the reader is actually talking to.

   The fix runs through the deploy script: `update.sh` records the commit it
   is about to build into `.env` (which `docker-compose.yml` passes to the
   `api` service as `env_file`), the API publishes it as the `commit` field of
   the unauthenticated `GET /api/v1/status`, and the offer page turns that
   value into a download URL. No human in the loop.

   The recording has to be correct, not merely present: it must REPLACE an
   existing value rather than append a second one, it must survive a `.env`
   with CRLF endings, and it must survive a `.env` with no trailing newline.
   A `.env` carrying two `DEPLOYED_COMMIT` lines would make the value
   ambiguous, and the whole point of the value is that it is unambiguous.

2. (Ollama, panel #4) When the source offer is not being served, the script
   restarts caddy -- and then checked ONLY the offer. Nothing verified that
   the REST of the site still answered, so a restart that left the whole site
   down was reported as a successful deploy. The site root is checked too now,
   with a BOUNDED retry (a fixed number of attempts) and a LOUD failure: a
   red error and `exit 1`. Never a silent success, never an unbounded loop.

The behavioural tests below run the REAL `record_deployed_commit` function out
of `update.sh` under bash, against real `.env` files. A copy of the logic
would prove nothing about the script (AGENTS.md: "Verify against the real
function, not a reimplementation").
"""

import base64
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UPDATE_SH = REPO_ROOT / "update.sh"
COMPOSE_YML = REPO_ROOT / "docker-compose.yml"

# The commit the deploy script is expected to record. Any 40-hex sha will do:
# the value is whatever `git rev-parse HEAD` returns on the deploy host.
SHA = "0123456789abcdef0123456789abcdef01234567"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def script() -> str:
    assert UPDATE_SH.is_file(), "update.sh must exist"
    return UPDATE_SH.read_text(encoding="utf-8")


def _extract_function(script: str, name: str) -> str:
    """The text of one shell function, lifted out of update.sh verbatim."""
    match = re.search(
        rf"(?ms)^{re.escape(name)}\(\) \{{\n.*?^\}}$", script
    )
    assert match, f"update.sh has no {name}() function"
    return match.group(0)


def _run_bash(body: str) -> str:
    """Run a bash script and return its stdout.

    The script is fed over STDIN (`bash -s`) rather than as `bash -c "<script>"`:
    a command-line argument is re-quoted by the Windows->WSL launcher and the
    quoting inside the script does not survive it. Everything the script
    touches lives in a temp dir INSIDE the shell, so nothing depends on how
    the host translates paths.
    """
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available on this machine")
    # The script is sent as BYTES: in text mode Windows rewrites every "\n"
    # to "\r\n" on the way into the pipe, and bash then sees a stray carriage
    # return on every line.
    proc = subprocess.run(
        [bash, "-s"],
        input=body.encode("utf-8"),
        capture_output=True,
        timeout=180,
    )
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    assert proc.returncode == 0, (
        f"the bash snippet failed (rc={proc.returncode})\n"
        f"stdout:\n{stdout}\nstderr:\n{stderr}"
    )
    return stdout


def _record(env_bytes, sha: str = SHA, runs: int = 1) -> bytes:
    """Run the REAL record_deployed_commit() from update.sh; return the .env bytes.

    `git` is stubbed with a shell function, so the script's own
    `git rev-parse HEAD` call is what decides the recorded value.
    """
    function = _extract_function(
        UPDATE_SH.read_text(encoding="utf-8"), "record_deployed_commit"
    )
    seed = ""
    if env_bytes is not None:
        encoded = base64.b64encode(env_bytes).decode("ascii")
        # An ABSOLUTE path: the .env under test is written inside the temp
        # dir, never relative to whatever directory the shell happens to
        # start in (which, under the mutation harness, is the repo).
        seed = f"printf '%s' '{encoded}' | base64 -d > \"$WORK/.env\"\n"
    body = (
        "set -e\n"
        'WORK=$(mktemp -d) || exit 1\n'
        "trap 'rm -rf \"$WORK\"' EXIT\n"
        'cd "$WORK" || exit 1\n'
        f"{seed}"
        f"{function}\n"
        f"git() {{ echo '{sha}'; }}\n"
        + "record_deployed_commit\n" * runs
        + "base64 \"$WORK/.env\" | tr -d '\\n'\n"
    )
    return base64.b64decode(_run_bash(body).strip())


# --------------------------------------------------------------------------
# The deploy script records the commit it is about to build
# --------------------------------------------------------------------------

class TestTheDeployScriptRecordsTheCommitItBuilds:
    def test_the_script_records_the_commit_from_git_rev_parse_head(self, script: str):
        assert "DEPLOYED_COMMIT=$(git rev-parse HEAD)" in script, (
            "the deploy script must take the commit it is about to build from "
            "`git rev-parse HEAD`, not from anything it invents"
        )

    def test_the_recording_happens_after_the_reset_and_before_the_build(
        self, script: str
    ):
        """The recorded commit must be the tree that is ABOUT TO be built.

        Taken after `git reset --hard origin/main`, it is the tip of `main`.
        Taken before the rebuild, it is the commit whose tree the new
        container will be built from.
        """
        reset = script.index("git reset --hard origin/main")
        call = re.search(r"(?m)^record_deployed_commit$", script)
        assert call, "update.sh never calls record_deployed_commit"
        build = script.index("docker compose up -d --build")
        assert reset < call.start() < build, (
            "record_deployed_commit must run after the reset to origin/main "
            f"(at {reset}) and before the rebuild (at {build}); it runs at "
            f"{call.start()}"
        )

    def test_the_recording_writes_to_the_env_file_the_api_service_reads(
        self, script: str
    ):
        """The value only reaches the container if it goes into .env.

        docker-compose.yml passes .env to the api service as `env_file`, so
        the recorded value travels WITH the container -- and, because it is
        written before the build, a failed build leaves the old container
        reporting the old commit, which is the truth.
        """
        assert ".env" in _extract_function(script, "record_deployed_commit"), (
            "record_deployed_commit must write the commit into .env"
        )

        compose = COMPOSE_YML.read_text(encoding="utf-8")
        api_block = re.search(r"(?ms)^  api:\n(.*?)(?=^  \w)", compose)
        assert api_block, "docker-compose.yml has no api service block"
        assert "env_file: .env" in api_block.group(1), (
            "the api service must read .env, or the recorded commit never "
            "reaches the running service"
        )


# --------------------------------------------------------------------------
# The recording is correct
# --------------------------------------------------------------------------

class TestTheRecordedValueIsCorrect:
    def test_a_missing_env_file_is_created_with_the_commit(self):
        assert _record(None) == f"DEPLOYED_COMMIT={SHA}\n".encode()

    def test_the_commit_is_appended_when_the_key_is_absent(self):
        result = _record(b"PUBLIC_BASE_URL=https://example.test\n")
        assert result == (
            b"PUBLIC_BASE_URL=https://example.test\n"
            + f"DEPLOYED_COMMIT={SHA}\n".encode()
        )

    def test_an_existing_value_is_replaced_not_appended(self):
        """Two DEPLOYED_COMMIT lines would make the published commit ambiguous."""
        result = _record(
            b"DEPLOYED_COMMIT=0000000000000000000000000000000000000000\n"
            b"PUBLIC_BASE_URL=https://example.test\n",
        )
        assert result == (
            f"DEPLOYED_COMMIT={SHA}\n".encode()
            + b"PUBLIC_BASE_URL=https://example.test\n"
        )
        assert result.count(b"DEPLOYED_COMMIT=") == 1, (
            "the commit must be recorded exactly once; it was recorded "
            f"{result.count(b'DEPLOYED_COMMIT=')} times"
        )

    def test_recording_twice_leaves_exactly_one_commit_line(self):
        """The deploy script runs again on every deploy; it must be idempotent."""
        result = _record(b"PUBLIC_BASE_URL=https://example.test\n", runs=2)
        assert result.count(b"DEPLOYED_COMMIT=") == 1, (
            "recording the commit twice must leave ONE line; it left "
            f"{result.count(b'DEPLOYED_COMMIT=')}"
        )
        assert f"DEPLOYED_COMMIT={SHA}\n".encode() in result

    def test_a_crlf_env_file_keeps_its_carriage_returns(self):
        """A Windows-edited .env must not have its endings rewritten."""
        result = _record(
            b"PUBLIC_BASE_URL=https://example.test\r\n"
            b"DEPLOYED_COMMIT=0000000000000000000000000000000000000000\r\n",
        )
        assert result == (
            b"PUBLIC_BASE_URL=https://example.test\r\n"
            + f"DEPLOYED_COMMIT={SHA}\r\n".encode()
        ), (
            "the CRLF line endings of .env must be preserved; the recorded "
            f"file was {result!r}"
        )

    def test_an_env_file_without_a_trailing_newline_still_gets_the_commit(self):
        result = _record(b"PUBLIC_BASE_URL=https://example.test")
        assert result == (
            b"PUBLIC_BASE_URL=https://example.test\n"
            + f"DEPLOYED_COMMIT={SHA}\n".encode()
        )


# --------------------------------------------------------------------------
# A failed caddy restart must not look like a successful deploy
# --------------------------------------------------------------------------

class TestTheDeployChecksTheWholeSiteAfterTheRestart:
    def test_the_script_checks_the_site_root(self, script: str):
        assert "site_root_is_live" in script, (
            "the deploy script must check that the site root still answers"
        )

    def test_the_root_check_runs_after_the_caddy_restart(self, script: str):
        restart = script.index("docker compose restart caddy")
        call = re.search(r"(?m)^\s*if site_root_is_live; then$", script)
        assert call, "update.sh never calls site_root_is_live"
        assert restart < call.start(), (
            "the site root must be checked AFTER the caddy restart (at "
            f"{restart}); it is checked at {call.start()}"
        )

    def test_the_root_check_runs_inside_the_restart_branch(self, script: str):
        """It is the RESTART that can take the site down, so that is where
        the rest of the site has to be checked."""
        branch = re.search(
            r"(?ms)^if \[ \"\$OFFER_LIVE\" -eq 0 \]; then\n(.*?)^fi$", script
        )
        assert branch, "update.sh has no OFFER_LIVE branch"
        assert "site_root_is_live" in branch.group(1), (
            "the site root must be checked in the branch that restarts caddy"
        )

    def test_the_retry_is_bounded(self, script: str):
        """An unbounded retry would hang the deploy instead of failing it."""
        assert not re.search(r"(?m)^\s*while\b", script), (
            "update.sh must not contain an unbounded `while` loop"
        )
        assert not re.search(r"(?m)^\s*until\b", script), (
            "update.sh must not contain an unbounded `until` loop"
        )
        assert not re.search(r"(?m)^\s*for\b[^\n]*;\s*true", script), (
            "update.sh must not contain an unbounded `for` loop"
        )
        assert re.search(r"(?m)^\s*for _ in 1 2 3; do$", script), (
            "the retries must be a fixed, bounded number of attempts"
        )

    def test_a_failed_root_check_fails_loudly(self, script: str):
        start = script.index("SITE_ROOT_LIVE=0")
        block = script[start:]
        block = block[: block.index("exit 1") + len("exit 1")]
        assert "site_root_is_live" in block, (
            "the bounded retry must actually call site_root_is_live"
        )
        assert "${RED}" in block, (
            "a failed site-root check must be reported in red, not silently"
        )

    def test_the_source_offer_check_still_checks_the_body(self, script: str):
        """The offer is a static file: a status code alone proves nothing.

        The React SPA catch-all answers 200 with the app shell, so the BODY is
        what tells the offer apart from the shell.
        """
        function = _extract_function(script, "source_offer_is_live")
        assert "DiverseInc/DepthSight" in function, (
            "the source offer check must grep the response BODY for the "
            "repository address, not only look at the status code"
        )
        assert "$PUBLIC_BASE_URL/source" in function

    def test_the_script_restarts_nothing_but_caddy(self, script: str):
        """This fix must not add a new restart to the deploy path."""
        restarts = set(re.findall(r"docker compose restart\s+(\S+)", script))
        assert restarts == {"caddy"}, (
            f"update.sh restarts {sorted(restarts)}; it must restart caddy only"
        )
