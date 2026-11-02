"""Alembic must connect with the driver that is actually installed.

THE DEFECT
----------
``alembic/env.py`` used to hand Alembic this URL::

    DATABASE_URL.replace("+asyncpg", "")

``api/database.py`` builds ``DATABASE_URL`` as
``postgresql+asyncpg://<user>:<pass>@<host>:<port>/<db>``, so stripping
``"+asyncpg"`` leaves the BARE ``postgresql://`` scheme.

On SQLAlchemy 2.x a bare ``postgresql://`` is not "no driver" -- it is
"pick one", and it picks **psycopg3**, which is not installed
(``requirements.txt`` ships only ``psycopg2-binary``).  So
``alembic upgrade head`` died with::

    ModuleNotFoundError: No module named 'psycopg'

``docker-startup.sh`` swallows that failure, so the API came up and served
traffic while the schema NEVER advanced.  A latent deploy bomb: the next
change that adds a migration deploys and silently does not migrate.

THE FIX
-------
Name the driver explicitly (``postgresql+psycopg2://``) instead of stripping
it, via the testable helper ``api.database.alembic_database_url()``.

THE OTHER HALF
---------------
``alembic.ini`` also carried a database password, in a PUBLIC repository, in
the git history since 2026-06-08.  The value is REDUNDANT (env.py overrides
``sqlalchemy.url``) and it was rotated on the live box on 2026-10-30, so it
is stale rather than live -- but a stale password published in a public
repository is a phishing lure and belongs in nobody's history.  These tests
assert on the SHAPE of the file (is there a password? is there a
credential-shaped URL?) and never on the value itself.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import re
import sys
import types

import pytest
from sqlalchemy.engine import make_url

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
ALEMBIC_ENV = REPO_ROOT / "alembic" / "env.py"

# A credential-shaped URL: scheme://user:password@host
CREDENTIAL_URL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+:[^\s:/@]+@")


def _alembic_database_url():
    """The helper, imported lazily.

    Imported inside the function so this file still COLLECTS against the
    pre-fix tree: the other tests must be able to fail with a real
    assertion instead of the whole module failing to import.
    """
    from api.database import alembic_database_url

    return alembic_database_url


# ---------------------------------------------------------------------------
# the helper
# ---------------------------------------------------------------------------


def test_the_alembic_url_uses_the_psycopg2_driver():
    """The URL Alembic gets must name psycopg2, the driver that IS installed."""
    url = _alembic_database_url()()

    assert url, "the helper must return a URL"
    # The URL is never echoed: it carries a password.
    has_asyncpg = "+asyncpg" in url
    assert not has_asyncpg, (
        "the Alembic URL must not keep the async driver: Alembic runs "
        "synchronously."
    )
    is_bare_scheme = url.startswith("postgresql://")
    assert not is_bare_scheme, (
        "the Alembic URL must never use the BARE 'postgresql://' scheme: on "
        "SQLAlchemy 2.x that resolves to psycopg3, which is not installed."
    )
    driver = make_url(url).get_driver_name()
    assert driver == "psycopg2", (
        f"the Alembic URL must resolve to the psycopg2 driver. Got driver: {driver!r}"
    )


def test_the_helper_converts_the_async_url_to_the_sync_one():
    """The rewrite is explicit: asyncpg -> psycopg2, everything else kept."""
    source = "postgresql+asyncpg://someuser:somepass@db.internal:5432/somedb"

    converted = _alembic_database_url()(source)

    assert converted == "postgresql+psycopg2://someuser:somepass@db.internal:5432/somedb", (
        "the helper must swap the driver and change nothing else "
        "(the assertion values here are synthetic, so they are safe to show)"
    )


def test_the_url_helper_does_not_raise_when_the_env_vars_are_present():
    """With the env vars present the helper must be callable and complete."""
    for name in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_HOST", "POSTGRES_DB"):
        assert os.environ.get(name), (
            f"the test environment must provide {name}; without it "
            "api.database refuses to import"
        )

    url = _alembic_database_url()()

    assert url.startswith("postgresql+psycopg2://"), (
        f"the helper must return the sync-driver URL. Got: {url!r}"
    )


# ---------------------------------------------------------------------------
# alembic.ini must carry no credential
# ---------------------------------------------------------------------------


def test_alembic_ini_carries_no_password():
    """alembic.ini must not hold a password.

    Asserted on the PARSED URL's password being None -- the literal value is
    never written into this test file, and never printed.
    """
    text = ALEMBIC_INI.read_text(encoding="utf-8")

    urls = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("sqlalchemy.url"):
            value = stripped.split("=", 1)[1].strip()
            if value:
                # The label is the KEY only. The value must never reach an
                # assertion message, or pytest would print it.
                urls.append(("sqlalchemy.url", value))

    for label, value in urls:
        try:
            parsed = make_url(value)
            has_password = parsed.password is not None
        except Exception as exc:  # noqa: BLE001 - the message must not echo the URL
            has_password = True
            detail = type(exc).__name__
        else:
            detail = "a password component"
        # The result is held in a BOOL on purpose: pytest's assertion
        # rewriting would otherwise print the value of the right-hand side,
        # and that value is the credential.
        assert not has_password, (
            f"{label} must not carry a password ({detail}). The URL comes from "
            "the environment variables via api.database.alembic_database_url(); "
            "this repository is PUBLIC and the value would live in git "
            "history for ever."
        )


def test_alembic_ini_carries_no_credential_shaped_url():
    """Nothing in alembic.ini may look like 'scheme://user:password@host'."""
    text = ALEMBIC_INI.read_text(encoding="utf-8")

    offenders = [
        n
        for n, line in enumerate(text.splitlines(), start=1)
        if CREDENTIAL_URL_RE.search(line)
    ]

    assert offenders == [], (
        "alembic.ini must not contain a credential-shaped URL. Offending "
        f"lines: {offenders!r}"
    )


def test_alembic_ini_still_points_at_the_url_from_the_environment():
    """The ini must not be the place the URL comes from -- env.py overrides it.

    This is what makes the empty `sqlalchemy.url` safe: alembic/env.py always
    sets the option from the environment before Alembic reads it.
    """
    env_source = ALEMBIC_ENV.read_text(encoding="utf-8")

    assert "alembic_database_url()" in env_source, (
        "alembic/env.py must take the URL from api.database."
        "alembic_database_url(); that is what overrides the ini value."
    )


# ---------------------------------------------------------------------------
# the integration point: what env.py actually hands to Alembic
# ---------------------------------------------------------------------------


class _FakeAlembicConfig:
    """Records what env.py would hand to Alembic."""

    def __init__(self):
        self.main_options = {}
        # None so env.py skips fileConfig() (which would re-read the ini).
        self.config_file_name = None

    def set_main_option(self, name, value):
        self.main_options[name] = value

    def get_main_option(self, name, default=None):
        return self.main_options.get(name, default)


class _FakeAlembicContext:
    """Stands in for `alembic.context` so env.py can be executed offline."""

    def __init__(self):
        self.config = _FakeAlembicConfig()
        self.configured = {}

    def is_offline_mode(self):
        return True

    def configure(self, **kwargs):
        self.configured.update(kwargs)

    def begin_transaction(self):
        return contextlib.nullcontext()

    def run_migrations(self):
        return None


def test_alembic_env_hands_alembic_the_psycopg2_url(monkeypatch):
    """THE CASE: the URL env.py actually passes must be a psycopg2 URL.

    The helper being correct is not enough -- env.py has to USE it.  This
    executes the real alembic/env.py against a fake `alembic.context`, so
    the assertion is on what Alembic would really be given.
    """
    fake_context = _FakeAlembicContext()
    fake_alembic = types.ModuleType("alembic")
    fake_alembic.context = fake_context
    monkeypatch.setitem(sys.modules, "alembic", fake_alembic)

    source = ALEMBIC_ENV.read_text(encoding="utf-8")
    namespace = {"__name__": "alembic_env_under_test", "__file__": str(ALEMBIC_ENV)}
    exec(compile(source, str(ALEMBIC_ENV), "exec"), namespace)

    assert "sqlalchemy.url" in fake_context.config.main_options, (
        "alembic/env.py must set the sqlalchemy.url option"
    )
    url = fake_context.config.main_options["sqlalchemy.url"]

    # The URL is never echoed: it carries a password.
    has_asyncpg = "+asyncpg" in url
    assert not has_asyncpg, "the URL Alembic gets must not keep the async driver"
    is_bare_scheme = url.startswith("postgresql://")
    assert not is_bare_scheme, (
        "the URL Alembic gets must never use the BARE 'postgresql://' "
        "scheme: on SQLAlchemy 2.x that resolves to psycopg3, which is not "
        "installed."
    )
    driver = make_url(url).get_driver_name()
    assert driver == "psycopg2", (
        f"the URL Alembic gets must resolve to the psycopg2 driver. Got driver: {driver!r}"
    )


@pytest.mark.parametrize(
    "bare_url",
    [
        "postgresql://u:p@localhost:5432/db",
    ],
)
def test_the_bare_scheme_really_resolves_to_the_missing_driver(bare_url):
    """Why the bare scheme is fatal, pinned so the reason cannot be forgotten.

    SQLAlchemy 2.x maps a bare 'postgresql://' to psycopg3.  This test does
    NOT import the driver -- it only asserts which driver the URL RESOLVES
    to, which is the fact that broke the migrations.
    """
    assert make_url(bare_url).get_driver_name() == "psycopg", (
        "a bare 'postgresql://' URL resolves to the psycopg3 driver on "
        "SQLAlchemy 2.x, and psycopg3 is not installed. This is exactly why "
        "the driver name must be stated explicitly."
    )
    assert make_url(bare_url).get_driver_name() != "psycopg2", (
        "the bare scheme must not resolve to psycopg2"
    )
