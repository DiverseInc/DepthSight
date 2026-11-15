"""The PUBLIC_BASE_URL read in update.sh must survive a .env with CRLF endings.

WHY THIS EXISTS
---------------
`update.sh` reads `PUBLIC_BASE_URL` out of `.env` and uses it as a URL in the
new AGPL source-offer gate:

    PUBLIC_BASE_URL=$(grep -E '^PUBLIC_BASE_URL=' .env ... | cut -d= -f2- ...)
    curl -s --max-time 15 "$PUBLIC_BASE_URL/source" ...

A .env may carry CRLF line endings: the deploy stamp script deliberately
PRESERVES whatever endings a .env already has ("a .env must not lose its
carriage returns, and an LF one must not gain any"), and the one-click
installer only sanitizes the four canonical filenames it creates. A trailing
carriage return inside a URL breaks the request, and the failure would be
REPORTED AS A CERTIFICATE PROBLEM -- "The TLS certificate at ... did not
verify" -- when the certificate is perfectly fine. That sends the operator
looking in the wrong place, and it aborts the deploy before the caddy restart.

The two sibling .env reads in the same code already strip the carriage return:
the `DEPLOYED_COMMIT` read in update.sh, and the `STAMPED_COMMIT` read in
ops/scripts/smoke-test.sh. This read is the third, and it is the one that
builds a URL.

HOW IT IS PROVED
----------------
BEHAVIOURALLY, by running update.sh's OWN read against a .env that really has
CRLF endings, in a real bash, and looking at the value it produces. Nothing
here greps update.sh's source for a string: a source-string check cannot tell
whether the read produces a clean URL.

AND THE TEST PROVES IT CAN FAIL: the same read WITHOUT the carriage-return
strip is run on the same file, and it MUST produce a value carrying the
carriage return. If that control ever stops holding, the assertion above has
gone vacuous and this file says so instead of passing quietly.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UPDATE_SH = REPO_ROOT / "update.sh"

# The URL the .env in the test carries. It is a placeholder that cannot
# resolve; nothing here touches the network.
EXPECTED_URL = "https://public-base-url.example.test"

# A .env with CRLF endings, exactly as a Windows-authored or Windows-copied
# file would have.
CRLF_ENV = (
    f"PUBLIC_BASE_URL={EXPECTED_URL}\r\n"
    "POSTGRES_USER=depthsight_user\r\n"
).encode("utf-8")

# The read WITHOUT the carriage-return strip, used only as the control.
READ_WITHOUT_STRIP = (
    "PUBLIC_BASE_URL=$(grep -E '^PUBLIC_BASE_URL=' .env 2>/dev/null "
    "| tail -1 | cut -d= -f2- || true)"
)


def _bash() -> str:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available on this machine")
    return bash


def _the_read(update_sh: Path) -> str:
    """update.sh's own PUBLIC_BASE_URL read, as the source line that does it.

    The read is found in the source rather than retyped here, so the test
    exercises the shipped pipeline: if that line is changed, the test changes
    with it.
    """
    text = update_sh.read_text(encoding="utf-8", errors="replace")
    lines = [ln for ln in text.splitlines()
             if re.match(r"^\s*PUBLIC_BASE_URL=\$\(", ln)]
    assert len(lines) == 1, (
        "expected exactly one PUBLIC_BASE_URL read in update.sh, found "
        f"{len(lines)}: {lines!r} -- this test can only follow the read if "
        "there is exactly one"
    )
    return lines[0]


def _run_read(work: Path, read_line: str) -> str:
    """Run a PUBLIC_BASE_URL read in `work`; return the value it produced.

    The read is fed to bash over STDIN as BYTES: as a command-line argument
    the Windows->WSL launcher re-quotes it and the quoting does not survive,
    and in text mode Windows rewrites every "\\n" to "\\r\\n" on the way into
    the pipe, so bash would see a carriage return on every line.

    The output is split on "\\n" ONLY, never with `str.splitlines()`: a bare
    carriage return is a line boundary to `splitlines()`, so splitting that way
    would silently EAT the very carriage return this test exists to detect --
    and the control below would then report that bash normalises line
    endings, which it does not.
    """
    body = (
        "set -e\n"
        'PUBLIC_BASE_URL=""\n'
        f"{read_line}\n"
        'printf \'VALUE=%s\\n\' "$PUBLIC_BASE_URL"\n'
    )
    proc = subprocess.run(
        [_bash(), "-s"],
        input=body.encode("utf-8"),
        capture_output=True,
        timeout=300,
        cwd=str(work),
    )
    out = proc.stdout.decode("utf-8", errors="replace")
    err = proc.stderr.decode("utf-8", errors="replace")
    assert proc.returncode == 0, (
        f"the read failed under bash (rc={proc.returncode}): {err!r}"
    )
    values = [ln[len("VALUE="):] for ln in out.split("\n")
              if ln.startswith("VALUE=")]
    assert values, f"the read printed no VALUE= line: {out!r}"
    return values[-1]


@pytest.fixture
def crlf_work(tmp_path):
    """A directory holding a .env with CRLF endings."""
    work = tmp_path / "work"
    work.mkdir()
    (work / ".env").write_bytes(CRLF_ENV)
    return work


def test_the_public_base_url_read_survives_a_crlf_env(crlf_work):
    """The value update.sh builds its URLs from must carry no carriage return."""
    value = _run_read(crlf_work, _the_read(UPDATE_SH))

    assert "\r" not in value, (
        "the PUBLIC_BASE_URL read returned a value carrying a carriage return: "
        f"{value!r} -- the offer check would then curl a broken URL and report "
        "a certificate failure that does not exist"
    )
    assert value == EXPECTED_URL, (
        f"the read returned {value!r}, expected {EXPECTED_URL!r}"
    )


def test_the_control_proves_a_carriage_return_reaches_the_value_without_the_strip(crlf_work):
    """The control: without the strip, the SAME read does carry the CR.

    If this ever stops holding, the assertion in the test above has gone
    vacuous -- the environment would be quietly normalising the line endings
    and the test would pass whatever update.sh did.
    """
    value = _run_read(crlf_work, READ_WITHOUT_STRIP)

    assert "\r" in value, (
        "the control did NOT produce a carriage return: "
        f"{value!r} -- so bash is normalising the line endings here and the "
        "test above cannot fail. That means the fix is untestable on this "
        "machine and the assertion must not be trusted."
    )
