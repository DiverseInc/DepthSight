# tests/test_the_promised_source_archive_is_fetched.py
"""The AGPL source offer PROMISES a downloadable archive. The gates must FETCH it.

THE DEFECT THIS PINS
--------------------
`frontend/source.html` tells the reader that

    https://github.com/DiverseInc/DepthSight/archive/<commit>.tar.gz

is "the exact tree of the running build", and that the `commit` field of
`GET /api/v1/status` names the commit the deployment was built from.

Nothing ever asked GitHub for that file. The deploy gate in `update.sh` and
the smoke test in `ops/scripts/smoke-test.sh` only asked whether the PAGE is
served, and grepped its body for the repository address. So an archive that
404s, an archive that is empty, and an archive of a commit that does not
exist all passed every gate -- and the offer's promise is exactly the thing
that was not being checked.

THE FIX, AND WHAT IS PINNED HERE
--------------------------------
Both gates now fetch the archive for the STAMPED commit (the same
`DEPLOYED_COMMIT` the api publishes as the `commit` field) and require an
HTTP 200 AND a non-empty body. A deployment that has no stamped commit --
one from before the stamp existed -- SKIPS the check with a clear message,
because there is nothing to check; a stamped commit whose archive does not
resolve FAILS the gate loudly, and says what is wrong.

These are BEHAVIOURAL tests: they run the real shell code against a stub
`curl` and look at what was requested, what came back, and what the gate
decided. A copy of the logic in the test would prove nothing about the
script (AGENTS.md: "Verify against the real function, not a reimplementation").

The stub `curl` answers from a table and RECORDS every request, so a test can
assert that the archive URL for the stamped commit was actually asked for --
which is the thing that was missing.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UPDATE_SH = REPO_ROOT / "update.sh"
SMOKE_TEST = REPO_ROOT / "ops" / "scripts" / "smoke-test.sh"

# The URL the offer promises. The gates have to ask for THIS url, for the
# stamped commit -- not for the branch tip, and not for something else.
ARCHIVE_URL = "https://github.com/DiverseInc/DepthSight/archive/{commit}.tar.gz"

STAMPED_COMMIT = "0123456789abcdef0123456789abcdef01234567"

# A stub curl: it answers from a table keyed by the URL, and it RECORDS
# every request it was asked to make. `-o`/`-w` are understood so the real
# scripts can be run against it unchanged.
STUB_CURL = """cat > bin/curl <<'CURLSTUB'
#!/bin/bash
out=""
fmt=""
url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2;;
    -w) fmt="$2"; shift 2;;
    --max-time) shift 2;;
    -*) shift;;
    *) url="$1"; shift;;
  esac
done
printf '%s\\n' "$url" >> "$CURL_LOG"
code=200
body="stub"
case "$url" in
  *"/archive/"*) code="$STUB_ARCHIVE_CODE"; body="$STUB_ARCHIVE_BODY";;
  *"/source"*) code=200; body="<html>DiverseInc/DepthSight</html>";;
  *"/ws"*) code=101; body="";;
esac
if [ -n "$out" ] && [ "$out" != "/dev/null" ]; then printf '%s' "$body" > "$out"; fi
if [ -z "$out" ]; then printf '%s' "$body"; fi
if [ -n "$fmt" ]; then printf '%s' "$code"; fi
exit 0
CURLSTUB
"""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _bash() -> str:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available on this machine")
    return bash


def _run(body: str) -> dict:
    """Run a bash script and report what happened.

    The script is fed over STDIN (`bash -s`) as BYTES: as a command-line
    argument the Windows->WSL launcher re-quotes it and the quoting does not
    survive, and in text mode Windows rewrites every "\\n" to "\\r\\n" on the
    way into the pipe, which bash then reads as a stray carriage return.
    """
    proc = subprocess.run(
        [_bash(), "-s"],
        input=body.encode("utf-8"),
        capture_output=True,
        timeout=300,
    )
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    rc = 255
    for line in stdout.splitlines():
        if line.startswith("RC="):
            rc = int(line[3:].strip() or "255")
    return {"rc": rc, "stdout": stdout, "stderr": stderr}


def _extract_function(script: str, name: str) -> str:
    """The text of one shell function, lifted out of the file verbatim."""
    match = re.search(rf"(?ms)^{re.escape(name)}\(\) \{{\n.*?^\}}$", script)
    assert match, f"the script has no {name}() function"
    return match.group(0)


def _requests(stdout: str) -> list:
    """The URLs the stub curl was asked for, as reported by the driver."""
    out = []
    for line in stdout.splitlines():
        if line.startswith("REQUEST "):
            out.append(line[len("REQUEST "):].strip())
    return out


# --------------------------------------------------------------------------
# The deploy gate (update.sh)
# --------------------------------------------------------------------------
def _drive_update_sh(env_lines: list, archive_code: str, archive_body: str) -> dict:
    """Run the REAL archive check of update.sh against a stubbed GitHub.

    The function is lifted out of `update.sh` verbatim and run in a
    throwaway directory that holds a real `.env`, so the `.env` parsing, the
    URL, the status check and the size check are all the script's own.
    """
    function = _extract_function(
        UPDATE_SH.read_text(encoding="utf-8"), "source_archive_is_available"
    )
    env = ""
    if env_lines:
        env = "printf '%s\\n' " + " ".join(
            "'" + line.replace("'", "'\\''") + "'" for line in env_lines
        ) + " > .env\n"

    body = (
        "set -u\n"
        "WORK=$(mktemp -d) || exit 1\n"
        "trap 'rm -rf \"$WORK\"' EXIT\n"
        "cd \"$WORK\" || exit 1\n"
        "mkdir -p bin\n"
        + STUB_CURL
        + "chmod +x bin/curl\n"
        "export PATH=\"$WORK/bin:$PATH\"\n"
        "export CURL_LOG=\"$WORK/curl.log\"\n"
        f"export STUB_ARCHIVE_CODE='{archive_code}'\n"
        f"export STUB_ARCHIVE_BODY='{archive_body}'\n"
        + env
        + "cat > fn.sh <<'FNFN'\n"
        + function
        + "\nFNFN\n"
        + ". ./fn.sh\n"
        + "if source_archive_is_available; then echo \"RC=0\"; else echo \"RC=$?\"; fi\n"
        + "if [ -f \"$CURL_LOG\" ]; then sed 's/^/REQUEST /' \"$CURL_LOG\"; fi\n"
    )
    result = _run(body)
    result["requests"] = _requests(result["stdout"])
    return result


class TestTheDeployGateFetchesTheArchive:
    def test_it_asks_for_the_archive_of_the_stamped_commit(self):
        """The URL the offer names is the thing that has to be requested."""
        result = _drive_update_sh(
            [f"DEPLOYED_COMMIT={STAMPED_COMMIT}"], "200", "tarball-bytes"
        )
        assert result["rc"] == 0, result["stdout"] + result["stderr"]
        assert result["requests"] == [ARCHIVE_URL.format(commit=STAMPED_COMMIT)], (
            f"the deploy gate requested {result['requests']}; it has to ask "
            "GitHub for the archive of the stamped commit"
        )

    def test_an_archive_that_is_not_there_fails_the_gate(self):
        """A 404 is the case the whole promise rests on."""
        result = _drive_update_sh(
            [f"DEPLOYED_COMMIT={STAMPED_COMMIT}"], "404", "Not Found"
        )
        assert result["rc"] != 0, (
            "the deploy gate accepted an archive that does not exist: "
            + result["stdout"]
        )

    def test_the_failure_says_what_is_wrong(self):
        """A gate that fails without saying why sends the operator hunting
        in the wrong place."""
        result = _drive_update_sh(
            [f"DEPLOYED_COMMIT={STAMPED_COMMIT}"], "404", "Not Found"
        )
        assert result["rc"] != 0
        assert "404" in result["stdout"], (
            "the failure does not report the status it got: " + result["stdout"]
        )
        assert STAMPED_COMMIT in result["stdout"], (
            "the failure does not name the commit whose archive is missing: "
            + result["stdout"]
        )

    def test_an_empty_archive_fails_the_gate(self):
        """A 200 with no bytes is not an archive."""
        result = _drive_update_sh([f"DEPLOYED_COMMIT={STAMPED_COMMIT}"], "200", "")
        assert result["rc"] != 0, (
            "the deploy gate accepted an EMPTY archive: " + result["stdout"]
        )
        assert "empty" in result["stdout"].lower(), (
            "the failure does not say the archive was empty: " + result["stdout"]
        )

    def test_a_commit_without_a_stamp_skips_the_check(self):
        """A deployment from before the stamp existed has nothing to check.

        That is not a failure: the check is SKIPPED, and -- this is the part
        that matters -- nothing is fetched at all.
        """
        result = _drive_update_sh(["PUBLIC_BASE_URL=https://example.invalid"], "404", "x")
        assert result["rc"] == 0, (
            "the deploy gate failed a deployment that has no stamped commit: "
            + result["stdout"]
            + result["stderr"]
        )
        assert result["requests"] == [], (
            "the deploy gate fetched something without a stamped commit: "
            + str(result["requests"])
        )
        assert "SKIP" in result["stdout"], (
            "the skip is not reported: " + result["stdout"]
        )


# --------------------------------------------------------------------------
# The smoke test (ops/scripts/smoke-test.sh)
# --------------------------------------------------------------------------
def _drive_smoke_test(env_lines: list, archive_code: str, archive_body: str) -> dict:
    """Run the REAL smoke test with a stubbed GitHub and a stubbed postgres.

    The whole script is run (it is copied into the shell verbatim), because
    the interesting part is what it DECIDES: its exit status is what carries
    a failure out of the `| tee` pipeline, and the log is what the operator
    reads.
    """
    script = SMOKE_TEST.read_text(encoding="utf-8", errors="replace")
    env = ""
    if env_lines:
        env = "printf '%s\\n' " + " ".join(
            "'" + line.replace("'", "'\\''") + "'" for line in env_lines
        ) + " > env\n"
    else:
        env = ": > env\n"

    body = (
        "set -u\n"
        "WORK=$(mktemp -d) || exit 1\n"
        "trap 'rm -rf \"$WORK\"' EXIT\n"
        "cd \"$WORK\" || exit 1\n"
        "mkdir -p bin state\n"
        + STUB_CURL
        + "printf '#!/bin/bash\\nexit 0\\n' > bin/psql\n"
        "chmod +x bin/curl bin/psql\n"
        "cat > smoke-test.sh <<'SMOKETESTSCRIPT'\n"
        + script
        + "\nSMOKETESTSCRIPT\n"
        + env
        + "export PATH=\"$WORK/bin:$PATH\"\n"
        "export CURL_LOG=\"$WORK/curl.log\"\n"
        f"export STUB_ARCHIVE_CODE='{archive_code}'\n"
        f"export STUB_ARCHIVE_BODY='{archive_body}'\n"
        "export STATE_DIR=\"$WORK/state\"\n"
        "export PUBLIC_BASE_URL=\"https://example.invalid\"\n"
        "export POSTGRES_PASSWORD=\"secret\"\n"
        "export DEPLOYED_COMMIT=\"\"\n"
        "export DEPLOY_ENV_FILE=\"$WORK/env\"\n"
        "bash smoke-test.sh\n"
        "echo \"RC=$?\"\n"
        "if [ -f \"$CURL_LOG\" ]; then sed 's/^/REQUEST /' \"$CURL_LOG\"; fi\n"
    )
    result = _run(body)
    result["requests"] = _requests(result["stdout"])
    return result


class TestTheSmokeTestFetchesTheArchive:
    def test_it_asks_for_the_archive_of_the_stamped_commit(self):
        result = _drive_smoke_test(
            [f"DEPLOYED_COMMIT={STAMPED_COMMIT}"], "200", "tarball-bytes"
        )
        assert result["rc"] == 0, result["stdout"] + result["stderr"]
        assert ARCHIVE_URL.format(commit=STAMPED_COMMIT) in result["requests"], (
            f"the smoke test requested {result['requests']}; it has to ask "
            "GitHub for the archive of the stamped commit"
        )

    def test_an_archive_that_is_not_there_fails_the_smoke_test(self):
        result = _drive_smoke_test(
            [f"DEPLOYED_COMMIT={STAMPED_COMMIT}"], "404", "Not Found"
        )
        assert result["rc"] != 0, (
            "the smoke test passed with an archive that does not exist: "
            + result["stdout"]
        )
        assert "FAIL" in result["stdout"], (
            "the smoke test failed without saying so in its log: " + result["stdout"]
        )

    def test_an_empty_archive_fails_the_smoke_test(self):
        result = _drive_smoke_test([f"DEPLOYED_COMMIT={STAMPED_COMMIT}"], "200", "")
        assert result["rc"] != 0, (
            "the smoke test passed with an EMPTY archive: " + result["stdout"]
        )

    def test_a_commit_without_a_stamp_skips_the_check(self):
        result = _drive_smoke_test(["PUBLIC_BASE_URL=https://example.invalid"], "404", "x")
        assert result["rc"] == 0, (
            "the smoke test failed a deployment that has no stamped commit: "
            + result["stdout"]
            + result["stderr"]
        )
        assert [r for r in result["requests"] if "/archive/" in r] == [], (
            "the smoke test fetched an archive without a stamped commit: "
            + str(result["requests"])
        )
        assert "SKIP" in result["stdout"], (
            "the skip is not reported: " + result["stdout"]
        )
