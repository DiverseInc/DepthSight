# File: tests/test_dockerignore_excludes_secrets.py
"""
The Docker build context was baking production secrets into the image layer.

Measured 2026-10-24 on ``origin/main`` (d6fe71d):

* ``.dockerignore`` excluded ``.git``, ``__pycache__/``, ``data/`` ... but NOT
  ``.env``, ``.env.*``, ``gcp_key.json``, ``*.key`` or ``*.pem``.
* ``.gitignore`` excluded ``gcp_key.json`` from GIT, but ``.dockerignore`` is a
  separate list -- so the file was still sent to the Docker build context.
* The root ``Dockerfile`` runs ``COPY . .`` (Dockerfile:23) AFTER ``pip
  install``, so every non-ignored file in the context is baked into an image
  layer.
* The production host (Elestio) has a real ``.env`` in its checkout and the
  CI/CD build runs from that checkout -- so REDIS_PASSWORD, POSTGRES_PASSWORD
  and the API keys were baked into the production image.

The runtime contract is NOT broken by excluding them:

* ``docker-compose.yml`` supplies ``.env`` to every container with
  ``env_file: .env`` -- Compose reads that file ON THE HOST and injects the
  values as container environment variables. The image never needs the file.
* ``docker-compose.yml`` bind-mounts ``./gcp_key.json:/app/gcp_key.json:ro``
  for the api/bot/celery_worker services -- the file is supplied at RUNTIME,
  not baked in. ``api/ai_assistant.py`` resolves ``gcp_key.json`` relative to
  the container working directory (``/app``), which is exactly where the bind
  mount puts it.
* The ``load_dotenv()`` calls in the app (``bot_runner.py``, ``api/auth.py``,
  ``api/security.py``, ...) are load-if-present: python-dotenv returns False
  when the file is missing and never raises. The values they read come from the
  environment Compose injected.

So the fix is to teach ``.dockerignore`` the same thing ``.gitignore`` already
knew, and to keep this test as the tripwire that stops it coming back.

One subtlety the test pins down: Docker compiles ``*`` to ``[^/]*``, so a bare
``*.key`` / ``*.pem`` matches the ROOT directory only -- ``certs/server.key``
and ``certs/live/fullchain.pem`` were measured shipping into the image until
the ``**/`` variants were added. The test therefore covers nested credential
material too, not just root-level files.
"""

import shutil
import subprocess
import tarfile
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERIGNORE = REPO_ROOT / ".dockerignore"

# Patterns that must stay in .dockerignore. These are the exact entries whose
# absence let production secrets reach the image layer.
REQUIRED_SECRET_PATTERNS = [
    ".env",
    ".env.*",
    "gcp_key.json",
    "*.key",
    "*.pem",
    # Docker compiles '*' to '[^/]*', so a bare '*.key' matches the ROOT only:
    # 'certs/server.key' shipped until the '**/' variants were added.
    "**/*.key",
    "**/*.pem",
    "**/.env",
    "*.env",
    # '*.env' matches the ROOT only, and the repo's own .gitignore ignores
    # '*.env' -- so a nested 'prod.env' is git-ignored but was still shipped.
    "**/*.env",
    "*keys*.json",
    "application_default_credentials.json",
]

# Files that must survive into the image, so the exclusion list cannot quietly
# turn into "exclude everything".
REQUIRED_IN_CONTEXT = [
    "requirements.txt",
    "api/__init__.py",
    "bot_module/__init__.py",
    "docker-startup.sh",
]

# Secret files that must NOT survive into the image.
SECRETS_MUST_BE_ABSENT = [
    ".env",
    ".env.local",
    "prod.env",
    "gcp_key.json",
    "server.key",
    "cert.pem",
    "certs/server.key",
    "certs/live/fullchain.pem",
    "id_rsa",
    "credentials.json",
    "service_account.json",
    "api_keys.json",
    "application_default_credentials.json",
    "secret.token",
    ".npmrc",
    "nested/.env",
    "nested/prod.env",
    "nested/gcp_key.json",
    "secrets/creds.json",
]

# The placeholder template is documentation, not a secret: it must still ship.
TEMPLATE_MUST_BE_PRESENT = [".env.example"]


def _ignore_lines() -> list[str]:
    """Return the effective (non-comment, non-blank) .dockerignore entries."""
    raw = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in raw if line.strip() and not line.strip().startswith("#")]


def test_dockerignore_lists_required_secret_patterns():
    """The secret patterns must be present as literal .dockerignore entries."""
    entries = _ignore_lines()
    missing = [pattern for pattern in REQUIRED_SECRET_PATTERNS if pattern not in entries]
    assert not missing, (
        f".dockerignore is missing secret exclusion(s): {missing}. "
        "Without them the Docker build context ships production secrets into "
        "the image via 'COPY . .'."
    )


def test_dockerignore_keeps_env_example_template():
    """'.env' and '.env.*' are excluded, but the placeholder template ships."""
    entries = _ignore_lines()
    assert ".env" in entries and ".env.*" in entries
    assert "!.env.example" in entries, (
        "'.env.example' holds placeholders only (change_me / replace_with_*), "
        "so it must be re-included with a '!' negation; deploy.sh copies it to "
        "'.env' on a fresh host."
    )
    # Docker applies the LAST matching pattern, so the negation must come after
    # the '.env.*' exclusion it overrides.
    assert entries.index("!.env.example") > entries.index(".env.*"), (
        "The '!.env.example' negation must come AFTER '.env.*' -- Docker lets "
        "the last matching pattern win."
    )


def _docker_available() -> bool:
    """True when a docker binary exists AND its daemon answers."""
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "info"], capture_output=True, text=True, timeout=60
    )
    return probe.returncode == 0


def test_docker_build_context_excludes_secrets(tmp_path):
    """
    Build a throwaway image from a context shaped like a production checkout
    and assert the secrets never reach the image.

    The real production Dockerfile is far too slow for a test (it installs
    build-essential and the Playwright browsers), so this uses a minimal
    'FROM scratch / COPY . /ctx' build -- the COPY layer is the only thing
    that decides what gets baked in.
    """
    # Checked here, not at import time, so collecting the suite never shells
    # out to docker.
    if not _docker_available():
        pytest.skip("a running docker daemon is required to test the build context")

    context = tmp_path / "context"
    context.mkdir()

    # The real .dockerignore under test, byte for byte.
    shutil.copyfile(DOCKERIGNORE, context / ".dockerignore")

    # A production checkout shape: real .env + gcp_key.json, plus the files
    # the image genuinely needs.
    (context / ".env").write_text(
        "SECRET_CANARY_DO_NOT_SHIP_ABC123=xyz\nREDIS_PASSWORD=canary\n",
        encoding="utf-8",
    )
    (context / ".env.local").write_text("SECRET_CANARY_DO_NOT_SHIP_ABC123=xyz\n", encoding="utf-8")
    (context / ".env.example").write_text("POSTGRES_PASSWORD=change_me\n", encoding="utf-8")
    (context / "gcp_key.json").write_text('{"type": "service_account"}\n', encoding="utf-8")
    (context / "server.key").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")
    (context / "cert.pem").write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    (context / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (context / "docker-startup.sh").write_text("#!/bin/bash\nexec \"$@\"\n", encoding="utf-8")
    (context / "api").mkdir()
    (context / "api" / "__init__.py").write_text("", encoding="utf-8")
    (context / "bot_module").mkdir()
    (context / "bot_module" / "__init__.py").write_text("", encoding="utf-8")
    (context / "nested").mkdir()
    (context / "nested" / ".env").write_text("SECRET_CANARY_DO_NOT_SHIP_ABC123=xyz\n", encoding="utf-8")
    (context / "nested" / "gcp_key.json").write_text('{"type": "service_account"}\n', encoding="utf-8")
    (context / "secrets").mkdir()
    (context / "secrets" / "creds.json").write_text('{"secret": "canary"}\n', encoding="utf-8")
    # Nested / oddly-named credential material: Docker's '*' does not cross
    # '/', so these need the '**/' variants to be excluded.
    (context / "certs").mkdir()
    (context / "certs" / "server.key").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")
    (context / "certs" / "live").mkdir()
    (context / "certs" / "live" / "fullchain.pem").write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    (context / "prod.env").write_text("SECRET_CANARY_DO_NOT_SHIP_ABC123=xyz\n", encoding="utf-8")
    (context / "id_rsa").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n", encoding="utf-8")
    (context / "credentials.json").write_text('{"secret": "canary"}\n', encoding="utf-8")
    (context / "service_account.json").write_text('{"secret": "canary"}\n', encoding="utf-8")
    (context / "secret.token").write_text("canary\n", encoding="utf-8")
    (context / ".npmrc").write_text("//registry.example/:_authToken=canary\n", encoding="utf-8")
    (context / "api_keys.json").write_text('{"secret": "canary"}\n', encoding="utf-8")
    (context / "application_default_credentials.json").write_text('{"secret": "canary"}\n', encoding="utf-8")
    (context / "nested" / "prod.env").write_text("SECRET_CANARY_DO_NOT_SHIP_ABC123=xyz\n", encoding="utf-8")

    (context / "Dockerfile").write_text("FROM scratch\nCOPY . /ctx\n", encoding="utf-8")

    tag = f"depthsight-dockerignore-test:{uuid.uuid4().hex[:12]}"
    container_id = None
    try:
        build = subprocess.run(
            ["docker", "build", "-t", tag, str(context)],
            capture_output=True,
            text=True,
            timeout=900,
        )
        assert build.returncode == 0, f"docker build failed:\n{build.stdout}\n{build.stderr}"

        # A scratch image has no CMD, so `docker create` needs a command; the
        # container is never started, it is only exported to read its contents.
        created = subprocess.run(
            ["docker", "create", tag, "true"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert created.returncode == 0, f"docker create failed:\n{created.stderr}"
        container_id = created.stdout.strip()

        exported = tmp_path / "image.tar"
        export = subprocess.run(
            ["docker", "export", container_id, "-o", str(exported)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert export.returncode == 0, f"docker export failed:\n{export.stderr}"

        with tarfile.open(exported) as tar:
            members = {name.lstrip("./") for name in tar.getnames()}
            present = {path for path in members if path.startswith("ctx/")}
            shipped = {path[len("ctx/"):] for path in present if path != "ctx/"}
            # Read every shipped file once: the canary must not appear in any
            # of them, not just in the ones whose names we already know.
            canary_hits = []
            for name in sorted(shipped):
                handle = tar.extractfile(f"ctx/{name}")
                if handle is None:
                    continue
                if b"SECRET_CANARY_DO_NOT_SHIP_ABC123" in handle.read():
                    canary_hits.append(name)

        leaked = sorted(set(SECRETS_MUST_BE_ABSENT) & shipped)
        assert not leaked, (
            f"Secret file(s) reached the Docker build context: {leaked}. "
            "The image would bake production secrets into a layer."
        )

        missing = sorted(set(REQUIRED_IN_CONTEXT) - shipped)
        assert not missing, (
            f"Required application file(s) were excluded from the build "
            f"context: {missing}. The exclusion list must not break the image."
        )

        missing_templates = sorted(set(TEMPLATE_MUST_BE_PRESENT) - shipped)
        assert not missing_templates, (
            f"Placeholder template(s) missing from the build context: "
            f"{missing_templates}. '.env.example' ships placeholders only and "
            "must stay available for a fresh-host bootstrap."
        )

        # The canary value itself must not be readable from any shipped file.
        assert not canary_hits, (
            f"The secret canary was found inside the image member(s) "
            f"{canary_hits}."
        )
    finally:
        if container_id:
            subprocess.run(
                ["docker", "rm", "-f", container_id],
                capture_output=True,
                text=True,
                timeout=300,
            )
        subprocess.run(
            ["docker", "rmi", "-f", tag], capture_output=True, text=True, timeout=300
        )
