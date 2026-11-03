"""A failed database migration must be LOUD, and must not change what the
container does.

THE DEFECT
----------
``docker-startup.sh`` is the ENTRYPOINT of this image (Dockerfile:30), and
every service built from it runs it: api, websocket, bot, market_data,
celery_worker and mcp-memory -- six containers.  The script ran::

    if flock -x "$LOCK_FILE" -c "alembic upgrade head"; then
        echo "Migrations check completed successfully."
    else
        echo "WARNING: Migrations failed. ..."
    fi

``set -e`` does not help: the failing command is the CONDITION of an ``if``,
so its exit status is consumed and the script carries on to ``exec "$@"``.
The container therefore comes up, looks healthy, and serves traffic against
a schema that was never migrated -- and the only trace of it is one WARNING
line among the startup logs.

WHAT THIS FILE PINS
-------------------
* a failed migration is reported as CRITICAL, on stderr, naming the exit
  code -- so it cannot be lost in the startup log;
* the application STILL starts afterwards: this is a loudness fix, NOT a
  behaviour change.  The container's exit status and the fact that the app
  runs are both unchanged (see the reasoning in the script itself);
* a SUCCESSFUL migration is not reported as CRITICAL.

The test runs the REAL script under bash with a stubbed ``alembic`` on
PATH, so it exercises the real control flow rather than a re-implementation.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
STARTUP_SCRIPT = REPO_ROOT / "docker-startup.sh"

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason=(
        "docker-startup.sh is a bash script and this test RUNS it under bash. "
        "The native Windows gate has no bash to run it with; the WSL gate and "
        "CI (ubuntu) do."
    ),
)


def _fake_tools(tmp_path: Path, *, alembic_exit_code: int) -> Path:
    """A PATH directory holding a stub ``alembic`` and a stub ``flock``.

    The stub ``flock`` runs the command it is given (``flock -c CMD``), so
    the script's own control flow -- the ``if`` and its ``else`` -- is the
    real one.
    """
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()

    # NOTE: the stub prints NO exit code of its own. If it did, the assertion
    # that the script reports the code would pass even if the script reported
    # nothing -- the test would be measuring the stub, not the script.
    alembic = fake_bin / "alembic"
    alembic.write_text(
        "#!/bin/sh\n"
        "echo 'stub alembic: running upgrade head'\n"
        f"exit {alembic_exit_code}\n",
        encoding="utf-8",
    )

    flock = fake_bin / "flock"
    flock.write_text(
        "#!/bin/sh\n"
        "for last; do :; done\n"
        'exec sh -c "$last"\n',
        encoding="utf-8",
    )

    for script in (alembic, flock):
        script.chmod(0o755)

    return fake_bin


def _run_startup(tmp_path: Path, *, alembic_exit_code: int) -> subprocess.CompletedProcess:
    fake_bin = _fake_tools(tmp_path, alembic_exit_code=alembic_exit_code)
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        ["bash", str(STARTUP_SCRIPT), "sh", "-c", "echo APP_STARTED"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_a_failed_migration_is_reported_as_critical_with_its_exit_code(tmp_path):
    """The failure must be CRITICAL, on stderr, and must name the exit code.

    NOTE (found by mutation S1): checking only that the word "CRITICAL" appears
    somewhere on stderr is TOO WEAK.  The script prints more than one CRITICAL
    line, so downgrading the migration report itself to a WARNING -- which is
    exactly the defect this file exists to catch -- still left "CRITICAL" on
    stderr and the weaker assertion passed.  So the assertion here is on the
    REPORTING LINES THEMSELVES: every line that reports the failed migration
    must be on stderr and must be CRITICAL.
    """
    proc = _run_startup(tmp_path, alembic_exit_code=3)

    reports = [
        line
        for line in proc.stderr.splitlines()
        if "migration" in line.lower() and "fail" in line.lower()
    ]
    assert reports, (
        "the failed migration must be reported at all. stderr was:\n"
        f"{proc.stderr}"
    )
    for line in reports:
        assert line.lstrip().upper().startswith("CRITICAL"), (
            "the report of a failed migration must be CRITICAL, not a WARNING "
            f"that scrolls away. Offending line: {line!r}"
        )
    # The exit code must be NAMED, next to the words that name it -- so that a
    # report which drops the code cannot pass by accident.
    assert re.search(r"\bexit code\s+3\b", proc.stderr), (
        "the CRITICAL report must name the migration's exit code (3). stderr "
        f"was:\n{proc.stderr}"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_a_failed_migration_still_starts_the_application(tmp_path):
    """The fix is loudness, NOT behaviour: the app must still start.

    A hard fail here would take down every container that shares this
    entrypoint, including the bot and the market-data service, so the
    container's behaviour is deliberately unchanged.
    """
    proc = _run_startup(tmp_path, alembic_exit_code=1)

    assert proc.returncode == 0, (
        "the startup script's own exit status must be unchanged (0): the app "
        f"still starts. Got {proc.returncode}. Output was:\n"
        f"{proc.stdout}{proc.stderr}"
    )
    assert "APP_STARTED" in proc.stdout, (
        "the application must still be started after a failed migration. "
        f"Output was:\n{proc.stdout}{proc.stderr}"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_a_successful_migration_is_not_reported_as_critical(tmp_path):
    """No false alarm: a migration that worked must not raise a CRITICAL."""
    proc = _run_startup(tmp_path, alembic_exit_code=0)

    combined = proc.stdout + proc.stderr
    assert "CRITICAL" not in combined, (
        "a successful migration must not be reported as CRITICAL. Output "
        f"was:\n{combined}"
    )
    assert proc.returncode == 0
    assert "APP_STARTED" in proc.stdout
