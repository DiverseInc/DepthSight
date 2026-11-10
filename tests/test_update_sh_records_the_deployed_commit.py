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

# The MANUAL deploy sequence in AGENTS.md has to RE-CREATE the api service and
# RESTART caddy, and it has to do so in that order. Both commands are pinned
# as whole lines, because a weakened variant (`up -d` without
# `--force-recreate`, `restart` of some other service) would read the same in
# a loose substring search.
RECREATE_API = "docker compose up -d --no-deps --force-recreate api"
RESTART_CADDY = "docker compose restart caddy"


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


def _run_bash_raw(body: str) -> dict:
    """Run a bash script and report its exit status and output.

    Unlike `_run_bash`, this does NOT insist on success: the fallback is
    expected to fail when no project directory exists, and that failure is
    what has to be inspected.
    """
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available on this machine")
    proc = subprocess.run(
        [bash, "-s"],
        input=body.encode("utf-8"),
        capture_output=True,
        timeout=180,
    )
    return {
        "rc": proc.returncode,
        "stdout": proc.stdout.decode("utf-8", errors="replace"),
        "stderr": proc.stderr.decode("utf-8", errors="replace"),
    }


def _run_fallback(call: str, keep: str) -> dict:
    """Run the REAL fallback command with the install locations pointed at
    fixture directories.

    `keep` says which of the two fixture directories exists:
      * "oneclick" -- only the one the one-click installer uses;
      * "both"     -- both of them;
      * "none"     -- neither of them.

    The command that runs is update.sh's own command, with only the two
    paths replaced, so what runs here is what runs on the deploy host.
    """
    function = _extract_function(
        UPDATE_SH.read_text(encoding="utf-8"), "resolve_project_dir"
    )
    remove = {
        "oneclick": 'rm -rf "$WORK/app-install"\n',
        "both": "",
        "none": 'rm -rf "$WORK/app-install" "$WORK/oneclick-install"\n',
    }[keep]
    body = (
        "set -u\n"
        "WORK=$(mktemp -d) || exit 1\n"
        "trap 'rm -rf \"$WORK\"' EXIT\n"
        "mkdir -p \"$WORK/app-install\" \"$WORK/oneclick-install\"\n"
        + function
        + "\n"
        + remove
        + call.replace("/opt/app/depthsight", '"$WORK/app-install"')
        .replace("/opt/depthsight", '"$WORK/oneclick-install"')
        + "\n"
        + 'echo "RESOLVED=${PROJECT_DIR:-}"\n'
    )
    result = _run_bash_raw(body)
    resolved = ""
    for line in result["stdout"].splitlines():
        if line.startswith("RESOLVED="):
            resolved = line[len("RESOLVED="):].strip()
    result["resolved"] = resolved
    return result


def _manual_deploy_block(agents: str) -> str:
    """The MANUAL deploy sequence in AGENTS.md, as the shell block it is.

    Scoped to that one fenced block on purpose: the prose around it explains
    the commands and may legitimately name them, and a search over the whole
    document would pin the WRONG thing.
    """
    start = agents.index("## Deploying to Elestio")
    fence = agents.index("```bash", start)
    end = agents.index("```", fence + len("```bash"))
    return agents[fence:end]


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


# --------------------------------------------------------------------------
# The MANUAL deploy in AGENTS.md must ACTIVATE what it just published
# --------------------------------------------------------------------------
class TestTheManualDeployActivatesTheStampAndTheRoute:
    """Two MEASURED gaps in the manual deploy sequence (review panel,
    2026-11-09), both of which made the AGPL offer point at the wrong thing.

    F1 -- the published commit could be STALE.
      `docker-compose.yml` gives the `api` service `env_file: .env`, and a
      container's environment is fixed when the container is CREATED. The
      manual deploy rebuilt and recreated only the services whose files
      changed, so on a BOT-ONLY deploy the `api` container kept publishing
      the PREVIOUS `DEPLOYED_COMMIT` while the `bot` already ran the new
      code -- and the archive the offer points at did not contain the running
      bot. The api service is therefore always RE-CREATED, so it picks up the
      stamp that was just written.

    F2 -- the new `/source` route never activated.
      `caddy.config` is bind-mounted into the caddy container
      (`docker-compose.yml:283`) and caddy reads its Caddyfile only at
      startup, so `docker compose up -d` does not activate a changed route.
      `frontend/` is bind-mounted too (`docker-compose.yml:289`), so
      `welcome.html` goes live the moment the pull lands and starts
      advertising the offer. Without a restart the site advertised `/source`
      while `/source` still fell through to the React SPA catch-all. Caddy is
      therefore always RESTARTED -- LAST, on purpose, so the route appears at
      the same moment the published commit becomes correct and never before.
    """

    @pytest.fixture(scope="class")
    def block(self) -> str:
        agents = AGENTS_MD.read_text(encoding="utf-8")
        return _manual_deploy_block(agents)

    def test_the_manual_deploy_recreates_the_api_service(self, block: str):
        """The stamp lives in the api's ENVIRONMENT, so the api container has
        to be re-created for the new value to exist inside it."""
        assert RECREATE_API in block, (
            "the manual deploy must re-create the api service ("
            + RECREATE_API
            + "); a container keeps the environment it was created with"
        )

    def test_the_manual_deploy_restarts_caddy(self, block: str):
        """caddy reads its bind-mounted Caddyfile only at startup."""
        assert RESTART_CADDY in block, (
            "the manual deploy must restart caddy (" + RESTART_CADDY + "); "
            "otherwise the new /source route never activates"
        )

    def test_the_api_recreate_comes_after_the_stamp(self, block: str):
        """The container must be re-created AFTER the value is written.

        The other way round the container would pick up the PREVIOUS value,
        and the offer would point at the commit before this deploy.
        """
        call = block.index(STAMP_INVOCATION)
        recreate = block.index(RECREATE_API)
        assert call < recreate, (
            f"the api must be re-created AFTER the stamp (at {call}); the "
            f"re-create is at {recreate}"
        )

    def test_the_caddy_restart_comes_after_the_api_recreate(self, block: str):
        """The route appears at the same moment the published commit becomes
        correct -- never before. That ordering is deliberate."""
        recreate = block.index(RECREATE_API)
        restart = block.index(RESTART_CADDY)
        assert recreate < restart, (
            f"caddy must be restarted AFTER the api is re-created (at "
            f"{recreate}); the restart is at {restart}"
        )

    def test_the_manual_deploy_never_discards_the_operator_s_work(self, block: str):
        """The deploy must not silently throw work away.

        The stamp REFUSES a dirty checkout instead, which is the safe
        behaviour: it stops and says what is wrong, and the operator decides.
        `git reset --hard` in the deploy would destroy that decision.
        """
        assert "git reset --hard" not in block, (
            "the manual deploy must not run `git reset --hard`; the stamp "
            "refuses a dirty checkout instead of discarding it"
        )


# --------------------------------------------------------------------------
# The deployment has to be ONE COMMIT
# --------------------------------------------------------------------------
# Three MEASURED gaps (review panel, 2026-11-09), all of which let the
# deployment run code that the archive the AGPL offer names does not
# contain.
#
# F6 -- update.sh never re-created the api.
#   `docker-compose.yml` gives the api service `env_file: .env`, and a
#   container's environment is fixed when the container is CREATED. The
#   deploy script only did `docker compose up -d --build`, which does NOT
#   re-create a container whose image id did not change -- so on a bot-only
#   deploy (or a cached build) the api kept publishing the PREVIOUS commit
#   while the rest of the deployment already ran the new code.
#
# F5 -- nothing fetched the promised archive.
#   (pinned in tests/test_the_promised_source_archive_is_fetched.py)
#
# F3 -- this PR moved the project directory and killed the one-click
#   installer's auto-update. `deploy.sh` clones into /opt/depthsight and its
#   cron runs `/opt/depthsight/update.sh` from a working directory that is
#   NOT a checkout, so update.sh takes the fallback branch -- and the
#   fallback pointed at /opt/app/depthsight, which does not exist on a
#   one-click host. The script exited 1 and nothing ever auto-updated again.
#
# F2 -- the MANUAL deploy could leave the deployment on two commits.
#   Six services build from the ROOT context and the api IMPORTS
#   bot_module, so rebuilding only the bot left the api, the market-data
#   service, the celery worker and the mcp-memory server on the old code
#   while the api published the new commit.
def _executable_commands(script: str) -> list:
    """The commands the script RUNS, in order.

    Comments are dropped on purpose: the deploy script explains what it
    does in prose, and an assertion that a COMMENT can satisfy says nothing
    about what the script does.
    """
    out = []
    for raw in script.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _compose_services_with_a_build(compose: str) -> list:
    """Every service in docker-compose.yml that is BUILT from a context.

    Those services run code that comes from THIS repository, so the
    deployment is only on one commit if every one of them is rebuilt and
    re-created from that commit.
    """
    services = []
    current = None
    for raw in compose.splitlines():
        header = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", raw)
        if header:
            current = header.group(1)
            continue
        if current and re.match(r"^\s+build:", raw):
            services.append(current)
    return services


def _service_names(line: str, verb: str) -> list:
    """The service names a `docker compose ...` command names."""
    tokens = line.split("#", 1)[0].split()
    assert verb in tokens, f"{line!r} is not a `docker compose {verb}` command"
    rest = tokens[tokens.index(verb) + 1:]
    return [t for t in rest if not t.startswith("-")]


def _lines_starting_with(block: str, prefix: str) -> list:
    return [
        line.strip()
        for line in block.splitlines()
        if line.strip().startswith(prefix)
    ]


class TestTheDeployRunsOnOneCommit:
    """update.sh: the api is re-created, and the fallback finds the project."""

    def test_update_sh_recreates_the_api_container(self, script: str):
        """The stamp lives in the api's ENVIRONMENT.

        A container keeps the environment it was CREATED with, so a
        container that is merely left running keeps publishing the previous
        commit -- while the rest of the deployment runs the new code.
        """
        commands = _executable_commands(script)
        assert RECREATE_API in commands, (
            "update.sh must re-create the api service (" + RECREATE_API + "); "
            "a container that is left running keeps the environment it was "
            "created with, so it keeps publishing the PREVIOUS commit"
        )

    def test_the_api_is_recreated_after_the_build(self, script: str):
        """The container has to be created from the NEW image, and with the
        NEW stamp in its environment."""
        commands = _executable_commands(script)
        builds = [
            i
            for i, c in enumerate(commands)
            if c.startswith("docker compose up -d --build")
        ]
        assert builds, "update.sh never builds the images"
        recreate = commands.index(RECREATE_API)
        assert recreate > builds[-1], (
            f"the api is re-created at {recreate} but the build is at "
            f"{builds[-1]}: the container has to be created from the new "
            "image, and it has to pick up the stamp that was just written"
        )

    def test_the_api_is_recreated_after_the_stamp(self, script: str):
        """Re-creating the container BEFORE the stamp is written gives it
        the previous value."""
        commands = _executable_commands(script)
        stamp = commands.index(STAMP_INVOCATION)
        recreate = commands.index(RECREATE_API)
        assert recreate > stamp, (
            f"the api is re-created at {recreate} but the stamp is written at "
            f"{stamp}: the container would pick up the PREVIOUS value"
        )

    def test_the_api_is_recreated_and_not_merely_restarted(self, script: str):
        """`docker compose restart` re-uses the container, so it keeps the
        environment it was created with. Only a re-create gives it a new
        one."""
        commands = _executable_commands(script)
        assert not any(
            re.search(r"docker compose restart\s+api\b", c) for c in commands
        ), "restarting the api does not give it the new environment"

    # -- the fallback project directory ------------------------------------
    def _fallback_call(self, script: str) -> str:
        """The command the fallback branch runs, lifted out of update.sh."""
        for line in script.splitlines():
            stripped = line.strip()
            if stripped.startswith("PROJECT_DIR=$(resolve_project_dir"):
                return stripped
        raise AssertionError(
            "update.sh has no fallback that resolves the project directory"
        )

    def test_the_fallback_knows_both_install_locations(self, script: str):
        """The one-click installer clones into /opt/depthsight; the
        maintainer's host keeps it in /opt/app/depthsight. The auto-update
        has to work on both, or it is dead on one of them."""
        call = self._fallback_call(script)
        assert "/opt/app/depthsight" in call, (
            "the fallback does not know /opt/app/depthsight: " + call
        )
        assert "/opt/depthsight" in call, (
            "the fallback does not know /opt/depthsight, which is where the "
            "one-click installer (deploy.sh) clones the project; the cron "
            "runs /opt/depthsight/update.sh, so the auto-update would be "
            "dead on every one-click host: " + call
        )

    def test_the_fallback_resolves_to_the_location_that_exists(self, script: str):
        """The second install location has to be USED when it is the one
        that exists -- not merely mentioned.

        The two paths in the command are pointed at fixture directories, so
        the command that runs is the one update.sh runs; only the paths
        differ. That is what makes this a behavioural test: whether the
        one-click location is honoured at all.
        """
        call = self._fallback_call(script)
        result = _run_fallback(call, keep="oneclick")
        assert result["rc"] == 0, result["stderr"]
        assert result["resolved"].endswith("/oneclick-install"), (
            "the fallback did not resolve to the only location that exists: "
            f"{result['resolved']!r}"
        )

    def test_the_fallback_prefers_the_first_location_when_both_exist(
        self, script: str
    ):
        call = self._fallback_call(script)
        result = _run_fallback(call, keep="both")
        assert result["rc"] == 0, result["stderr"]
        assert result["resolved"].endswith("/app-install"), (
            "the fallback resolved to "
            f"{result['resolved']!r} instead of the first location"
        )

    def test_the_fallback_fails_loudly_naming_both_locations(self, script: str):
        """When neither location exists the script has to stop AND say which
        paths it looked at -- otherwise the operator has nothing to act on."""
        call = self._fallback_call(script)
        result = _run_fallback(call, keep="none")
        assert result["rc"] != 0, (
            "the fallback succeeded although no project directory exists"
        )
        assert "app-install" in result["stderr"], (
            "the failure does not name the first location it looked at: "
            + result["stderr"]
        )
        assert "oneclick-install" in result["stderr"], (
            "the failure does not name the second location it looked at: "
            + result["stderr"]
        )


class TestTheManualDeployRunsOnOneCommit:
    """AGENTS.md's manual deploy must rebuild EVERY service from this repo."""

    @pytest.fixture(scope="class")
    def block(self) -> str:
        agents = AGENTS_MD.read_text(encoding="utf-8")
        return _manual_deploy_block(agents)

    @pytest.fixture(scope="class")
    def buildable(self) -> list:
        compose = COMPOSE_YML.read_text(encoding="utf-8")
        services = _compose_services_with_a_build(compose)
        assert services, (
            "docker-compose.yml declares no service with a build context, so "
            "this test cannot tell what the deploy has to rebuild"
        )
        return services

    def test_the_manual_deploy_rebuilds_every_service_from_this_repository(
        self, block: str, buildable: list
    ):
        """Six services build from the ROOT context and the api IMPORTS
        bot_module, so rebuilding only the bot leaves the api, the
        market-data service, the celery worker and the mcp-memory server
        running the OLD code while the api publishes the NEW commit."""
        builds = _lines_starting_with(block, "docker compose build")
        assert builds, "the manual deploy never builds"
        named = set()
        for line in builds:
            named.update(_service_names(line, "build"))
        assert set(buildable) <= named, (
            f"the manual deploy builds {sorted(named)}; the services built "
            f"from this repository are {sorted(buildable)} and every one of "
            "them has to be rebuilt, or the deployment runs on two commits "
            "and the archive the offer names does not contain the running code"
        )

    def test_the_manual_deploy_recreates_every_service_from_this_repository(
        self, block: str, buildable: list
    ):
        """Building is not enough: a service that is not re-created keeps
        running the image it was created from."""
        ups = [
            line
            for line in _lines_starting_with(block, "docker compose up -d --no-deps")
            if "--force-recreate" not in line
        ]
        assert ups, "the manual deploy never brings the services up"
        named = set()
        for line in ups:
            named.update(_service_names(line, "up"))
        assert set(buildable) <= named, (
            f"the manual deploy re-creates {sorted(named)}; the services "
            f"built from this repository are {sorted(buildable)} and every "
            "one of them has to be re-created from that commit"
        )

    def test_the_manual_deploy_names_no_placeholder(self, block: str):
        """A placeholder is how the deployment ends up on two commits."""
        agents = AGENTS_MD.read_text(encoding="utf-8")
        assert "<changed services>" not in agents, (
            "AGENTS.md still carries the '<changed services>' placeholder"
        )
        for line in _lines_starting_with(block, "docker compose build"):
            assert "<" not in line and ">" not in line, (
                f"the build command still carries a placeholder: {line!r}"
            )

    def test_the_manual_deploy_runs_in_the_right_order(self, block: str):
        """Stamp, build, re-create, restart caddy -- in that order.

        The stamp has to be written before the container is created (the
        value travels with the container), and caddy is restarted last (the
        route appears at the same moment the published commit becomes
        correct).
        """
        steps = [
            ("the stamp", STAMP_INVOCATION),
            ("the build", "docker compose build"),
            ("the api re-create", RECREATE_API),
            ("the caddy restart", RESTART_CADDY),
        ]
        positions = []
        for label, needle in steps:
            assert needle in block, f"the manual deploy has no {label}"
            positions.append((label, block.index(needle)))
        for (before_label, before), (after_label, after) in zip(
            positions, positions[1:]
        ):
            assert before < after, (
                f"{before_label} (at {before}) must come before {after_label} "
                f"(at {after})"
            )
