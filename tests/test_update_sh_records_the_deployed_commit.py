# tests/test_update_sh_records_the_deployed_commit.py
"""The deploy path must publish the commit it built, and must not call a
broken deploy a success.

THE DEFECTS THIS PINS
---------------------
Two MEASURED gaps, both found by the review panel on 2026-11-07 and both
fixed here.

1. (Copilot, panel #3, condition (a)) The AGPL-3.0 section 13 offer pointed
   the reader at the tip of `main` and told them to ASK BY EMAIL for the
   exact commit behind the running build. There was no machine-checkable link
   between the running build and its source, so the email request was
   load-bearing for the one thing section 13 asks for: the source of the
   version the reader is actually talking to.

   The fix runs through the deploy path: the commit is recorded into `.env`
   (which `docker-compose.yml` passes to the `api` service as `env_file`), the
   API publishes it as the `commit` field of the unauthenticated
   `GET /api/v1/status`, and the offer page turns that value into a download
   URL. No human in the loop.

2. (Ollama, panel #4) When the source offer is not being served, the script
   restarts caddy -- and then checked ONLY the offer. Nothing verified that
   the REST of the site still answered, so a restart that left the whole site
   down was reported as a successful deploy. The site root is checked too now,
   with a BOUNDED retry (a fixed number of attempts) and a LOUD failure: a
   red error and `exit 1`. Never a silent success, never an unbounded loop.

WHERE THE RECORDING LIVES NOW
-----------------------------
The recording used to be a shell FUNCTION inside `update.sh`, and the tests
below used to lift that function out of the file with a regex and run it.
That is no longer where it lives: the recording is now ONE script,
`ops/scripts/record_deployed_commit.sh`, shared by the automatic deploy
(`update.sh`) and the MANUAL deploy in AGENTS.md -- because the manual deploy
is the path the owner actually uses, and a fix that only fires on the path
nobody uses is decorative.

So the behaviour of the recording is tested in
`tests/test_record_deployed_commit_script.py`, against the real script. The
tests HERE pin the ARRANGEMENT: that there is exactly ONE implementation,
that both deploy paths call it, that it runs at the right moment, and that its
failure cannot be swallowed.

The behavioural tests that ran the real caddy/site-root functions out of
`update.sh` still do exactly that: a copy of the logic would prove nothing
about the script (AGENTS.md: "Verify against the real function, not a
reimplementation").
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UPDATE_SH = REPO_ROOT / "update.sh"
COMPOSE_YML = REPO_ROOT / "docker-compose.yml"
AGENTS_MD = REPO_ROOT / "AGENTS.md"
STAMP_SCRIPT = "ops/scripts/record_deployed_commit.sh"

# The ONE way the stamp is invoked. `bash <script>`, never `./<script>`:
# every tracked shell script in this repository is mode 100644 (there is not
# one 100755 file in it), so a bare `./...` fails with "Permission denied" on
# the deploy host -- which, under `set -e`, aborts the deploy, and in the
# manual path silently leaves the commit unstamped.
STAMP_INVOCATION = f"bash {STAMP_SCRIPT}"


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


# --------------------------------------------------------------------------
# There is ONE implementation of the stamp, and both deploy paths call it
# --------------------------------------------------------------------------
class TestThereIsOneImplementationAndBothPathsCallIt:
    def test_the_stamp_script_exists(self):
        assert (REPO_ROOT / STAMP_SCRIPT).is_file(), (
            f"{STAMP_SCRIPT} must exist: it is the one implementation of the "
            "commit stamp"
        )

    def test_update_sh_calls_the_shared_script_exactly_once(self, script: str):
        calls = [
            line
            for line in script.splitlines()
            if STAMP_SCRIPT in line and not line.strip().startswith("#")
        ]
        assert len(calls) == 1, (
            f"update.sh calls the stamp script {len(calls)} times: {calls}"
        )

    def test_update_sh_has_no_second_implementation_of_the_stamping(
        self, script: str
    ):
        """The stamping logic must exist in exactly ONE place.

        A second copy in `update.sh` would drift from the shared script, and
        the offer's claim would then be true of whichever copy ran.
        """
        assert "DEPLOYED_COMMIT=" not in script, (
            "update.sh still writes DEPLOYED_COMMIT itself; the stamping logic "
            "must live only in " + STAMP_SCRIPT
        )
        assert "git rev-parse HEAD" not in script, (
            "update.sh still takes the commit itself; the stamping logic must "
            "live only in " + STAMP_SCRIPT
        )

    def test_the_call_goes_through_bash_and_not_a_bare_path(self, script: str):
        """A bare `./` invocation cannot work: nothing in this repository is
        mode 100755, so on the deploy host it fails with "Permission denied"."""
        call = next(
            line
            for line in script.splitlines()
            if STAMP_SCRIPT in line and not line.strip().startswith("#")
        )
        assert call.strip() == STAMP_INVOCATION, (
            f"the stamp must be invoked as {STAMP_INVOCATION!r}; it is "
            f"{call.strip()!r}"
        )

    def test_the_agents_md_manual_deploy_calls_the_shared_script(self):
        """The manual deploy in AGENTS.md is the path the owner actually uses.

        The first version of this fix only ran inside `update.sh`, so on a
        manual deployment the commit field stayed EMPTY -- and the AGPL offer
        would have pointed at a commit the site was not running.
        """
        agents = AGENTS_MD.read_text(encoding="utf-8")
        assert STAMP_INVOCATION in agents, (
            "AGENTS.md's manual deploy sequence must call " + STAMP_INVOCATION
        )

    def test_the_manual_deploy_runs_the_stamp_after_the_pull_and_before_the_build(
        self,
    ):
        """The value must describe the tree that is about to be BUILT."""
        agents = AGENTS_MD.read_text(encoding="utf-8")
        call = agents.index(STAMP_INVOCATION)
        pull = agents.index("git pull origin main")
        build = agents.index("docker compose build")
        assert pull < call < build, (
            f"the stamp must run after the pull ({pull}) and before the build "
            f"({build}); it is at {call}"
        )

    def test_the_stamp_runs_after_the_reset_and_before_the_build(
        self, script: str
    ):
        """The recorded commit must be the tree that is ABOUT TO be built.

        Taken after `git reset --hard origin/main`, it is the tip of `main`.
        Taken before the rebuild, it is the commit whose tree the new
        container will be built from.
        """
        reset = script.index("git reset --hard origin/main")
        call = script.index(STAMP_INVOCATION)
        build = script.index("docker compose up -d --build")
        assert reset < call < build, (
            f"the stamp must run after the reset to origin/main (at {reset}) "
            f"and before the rebuild (at {build}); it runs at {call}"
        )

    def test_a_failed_stamp_cannot_be_swallowed(self, script: str):
        """A refusal must STOP the deploy, not be ignored.

        The stamp refuses a checkout that is not the committed tree, so that
        the offer's claim is true by construction. If the refusal were
        ignored, the deploy would build from a dirty tree and publish a claim
        that is false.
        """
        assert re.search(r"(?m)^set -e\b", script), (
            "update.sh must run with `set -e`, so a failed stamp stops the deploy"
        )
        call_line = next(
            line
            for line in script.splitlines()
            if STAMP_SCRIPT in line and not line.strip().startswith("#")
        )
        for swallow in ("|| true", "|| :", "2>/dev/null", "&"):
            assert swallow not in call_line, (
                f"the stamp call swallows its failure with {swallow!r}"
            )

    def test_the_value_reaches_the_container_through_the_env_file(
        self, script: str
    ):
        """The value only reaches the container if it goes into .env.

        docker-compose.yml passes .env to the api service as `env_file`, so
        the recorded value travels WITH the container -- and, because it is
        written before the build, a failed build leaves the old container
        reporting the old commit, which is the truth.
        """
        compose = COMPOSE_YML.read_text(encoding="utf-8")
        api_block = re.search(r"(?ms)^  api:\n(.*?)(?=^  \w)", compose)
        assert api_block, "docker-compose.yml has no api service block"
        assert "env_file: .env" in api_block.group(1), (
            "the api service must read .env, or the recorded commit never "
            "reaches the running service"
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
