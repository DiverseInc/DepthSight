# tests/test_production_bundle_excludes_demo_credentials.py
"""A production frontend bundle must not contain the seeded demo password.

THE DEFECT THIS PINS
--------------------
`frontend/src/pages/Login.tsx` held the seeded demo username and password in
MODULE-LEVEL CONSTANTS. The login page's demo panel was already gated behind
`isDemoCredentialsEnabled()` (frontend/src/lib/demoAccess.ts), so the panel
was not RENDERED in a production build -- but the strings were still compiled
into the shipped JavaScript. Anyone who opened devtools on the public login
page could read the demo password without clicking anything, and then log in
as the demo account.

The credentials now live in `frontend/demo-build-flag.ts`, which is imported
only by vite.config.ts and vitest.config.ts and is therefore NOT reachable
from the application entry point. They reach the app as a build-time `define`
that Vite substitutes with the literal `null` unless the build explicitly
asks for the demo helper (VITE_SHOW_DEMO=true). A production bundle does not
contain the strings AT ALL -- not even behind a runtime `if`.

WHAT THIS TEST DOES
-------------------
It greps the BUILT production bundle for those strings and fails if they are
present. It also greps `frontend/src`, because that is what the bundle is
made of: a credential sitting in a source file the bundler cannot eliminate
is the same defect one step earlier, and this check runs even when no bundle
has been built (the Python CI job does not build the frontend).

It then BUILDS the frontend three times -- and asserts both halves of the
truth:

  * a build with no opt-in contains neither string;
  * a build that opts in (in an ENV FILE) with the credentials in the
    BUILD ENVIRONMENT contains both;
  * a build whose ENVIRONMENT says VITE_SHOW_DEMO=true but whose env FILE
    does not opt in contains neither.

The second is the negative control. Without it, "the bundle has no
credentials" could be true for a reason that has nothing to do with this
change (a bundler that dropped the panel, a build that failed, a grep that
never matched anything) and the first half would prove nothing.

The third is why the opt-in comes from an env FILE and not from
`process.env`: Vite's `loadEnv()` merges `process.env` over the .env
files, so a stray VITE_SHOW_DEMO=true in the build environment (a CI
variable, a shell export, a compose build arg) used to be enough to
compile the credentials in. A .dockerignore can only exclude FILES, so it
could not stop that.

To run the full check:

    cd frontend && npm run build && cd ..
    python -m pytest tests/test_production_bundle_excludes_demo_credentials.py
"""

import contextlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = REPO_ROOT / "frontend"
SRC = FRONTEND / "src"
DIST = FRONTEND / "dist"

# The seeded demo account created by scripts/seed_demo_data.py.
FORBIDDEN = ("DemoPassword123", "alex_trader")

# The same account, as the BUILD ENVIRONMENT has to carry it now. The
# values are not in the source tree at all: frontend/ is bind-mounted
# read-only into the Caddy container that serves the public site
# (docker-compose.yml:289), so a plaintext credential must not sit in it.
DEMO_USERNAME = "alex_trader"
DEMO_PASSWORD = "DemoPassword123!"

# The build-environment variables the credentials come from.
DEMO_USERNAME_ENV = "VITE_DEMO_USERNAME"
DEMO_PASSWORD_ENV = "VITE_DEMO_PASSWORD"

# The env file the demo opt-in is read from. Vite reads .env, .env.local,
# .env.<mode> and .env.<mode>.local in that order, so this one has the
# highest precedence. It is gitignored (frontend/.gitignore excludes
# `*.local`) and excluded from the Docker build context
# (frontend/.dockerignore excludes `.env.*.local`), so it can opt a build
# in without changing a tracked file.
DEMO_ENV_FILE = FRONTEND / ".env.production.local"

# Files that end up in the bundle: the app sources, the Vite entry template
# and everything copied verbatim out of public/.
TEXT_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".css", ".html", ".json"}

# Directories under frontend/ that are not part of the build context.
SKIP_DIRS = {"node_modules", "dist", "coverage", ".git", ".vite"}

# A production build of this app takes well under a minute on a warm
# machine; three of them plus a slow machine still fit in ten minutes.
BUILD_TIMEOUT_SECONDS = 900


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")


def _is_test_file(path: Path) -> bool:
    """Test files are never bundled, so they may name the credential."""
    if "__tests__" in path.parts:
        return True
    return path.stem.endswith((".test", ".spec"))


def _bundled_source_files():
    """Every file the bundler could pick up."""
    yield from sorted(SRC.rglob("*"))
    yield FRONTEND / "index.html"
    public_dir = FRONTEND / "public"
    if public_dir.is_dir():
        yield from sorted(public_dir.rglob("*"))


def _frontend_tree_files():
    """Every file in the frontend build context.

    docker-compose.yml builds the frontend image with `context: ./frontend`,
    so this is the whole directory -- including frontend/demo-build-flag.ts,
    which is where the credentials used to live.
    """
    for root, dirnames, filenames in os.walk(FRONTEND):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        for filename in filenames:
            yield Path(root) / filename


def _built_assets():
    yield from sorted(DIST.rglob("*"))


# --------------------------------------------------------------------------
# Building the bundle for real
# --------------------------------------------------------------------------
def _npm_is_available() -> bool:
    return shutil.which("npm") is not None or shutil.which("npm.cmd") is not None


@contextlib.contextmanager
def _build_env(demo_flag: str | None):
    """The env files a build reads, under this build's control.

    The opt-in is written to DEMO_ENV_FILE, the env file with the highest
    precedence, so it decides whatever the developer's own local env files
    say. It is written as an explicit value -- "false" as well as "true" --
    so a build that is meant to be off is off no matter what is on disk.

    The previous contents are restored afterwards.
    """
    previous = None
    if DEMO_ENV_FILE.is_file():
        previous = DEMO_ENV_FILE.read_bytes()
        DEMO_ENV_FILE.unlink()
    try:
        DEMO_ENV_FILE.write_text(
            f"VITE_SHOW_DEMO={demo_flag or 'false'}\n", encoding="utf-8"
        )
        yield
    finally:
        if DEMO_ENV_FILE.is_file():
            DEMO_ENV_FILE.unlink()
        if previous is not None:
            DEMO_ENV_FILE.write_bytes(previous)


def _build_into(
    out_dir: Path, demo_flag: str | None, stray_flag: str | None = None
) -> None:
    """Run a real production build into `out_dir`.

    `demo_flag` is the value VITE_SHOW_DEMO takes in the ENV FILE for the
    build, or None for a build that does not opt in at all.

    `stray_flag` is the value VITE_SHOW_DEMO takes in the BUILD
    ENVIRONMENT, or None. It is the ambient value the opt-in has to
    ignore: Vite's `loadEnv()` merges `process.env` over the env files,
    so a stray variable here used to be enough to compile the seeded demo
    credentials into the bundle.

    The credentials are always passed through the build environment, in
    both cases: they come from the build environment now, and a build that
    does not opt in must not ship them.
    """
    env = dict(os.environ)
    env.pop("VITE_SHOW_DEMO", None)
    env.pop(DEMO_USERNAME_ENV, None)
    env.pop(DEMO_PASSWORD_ENV, None)
    env[DEMO_USERNAME_ENV] = DEMO_USERNAME
    env[DEMO_PASSWORD_ENV] = DEMO_PASSWORD
    if stray_flag is not None:
        env["VITE_SHOW_DEMO"] = stray_flag

    command = f'npm run build -- --outDir "{out_dir}" --emptyOutDir'
    with _build_env(demo_flag):
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(FRONTEND),
            env=env,
            capture_output=True,
            text=True,
            timeout=BUILD_TIMEOUT_SECONDS,
        )

    assert result.returncode == 0, (
        "the production build failed, so the bundle check could not run:\n"
        + (result.stdout or "")[-2000:]
        + (result.stderr or "")[-2000:]
    )


@pytest.fixture(scope="module")
def production_builds():
    """Three real production builds.

    * "without" -- the default build: no opt-in anywhere.
    * "with"    -- the env FILE opts in and the environment carries the
                   credentials: the demo helper must ship.
    * "stray"   -- the ENVIRONMENT says VITE_SHOW_DEMO=true and carries
                   the credentials, but no env FILE opts in: the demo
                   helper must NOT ship.
    """
    if not _npm_is_available():
        pytest.skip("npm is not available on this machine")
    if not (FRONTEND / "node_modules").is_dir():
        pytest.skip("frontend/node_modules is not installed")

    tmp = Path(tempfile.mkdtemp(prefix="depthsight-demo-build-"))
    try:
        without_demo = tmp / "without-demo"
        with_demo = tmp / "with-demo"
        stray = tmp / "stray-env"
        without_demo.mkdir()
        with_demo.mkdir()
        stray.mkdir()

        _build_into(without_demo, None)
        _build_into(with_demo, "true")
        _build_into(stray, None, stray_flag="true")

        yield {"without": without_demo, "with": with_demo, "stray": stray}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _needles_found(out_dir: Path, needles) -> tuple:
    """Which of `needles` occur in `out_dir`, and in which files.

    Both credential strings end up in the SAME bundle file, so the count that
    matters is the number of needles found, not the number of files.
    """
    found = set()
    files = []
    for path in sorted(out_dir.rglob("*")):
        if not path.is_file():
            continue
        text = _read(path)
        hit = False
        for needle in needles:
            if needle in text:
                found.add(needle)
                hit = True
        if hit:
            files.append(path.name)
    return found, files


class TestTheDemoCredentialsAreNotShipped:
    def test_no_frontend_source_file_contains_the_demo_credentials(self):
        offenders = []
        for path in _bundled_source_files():
            if not path.is_file():
                continue
            if path.suffix not in TEXT_SUFFIXES:
                continue
            if _is_test_file(path):
                continue
            text = _read(path)
            for needle in FORBIDDEN:
                if needle in text:
                    offenders.append(f"{path.relative_to(REPO_ROOT)} -> {needle}")

        assert not offenders, (
            "the seeded demo credentials are in a file the bundler can ship. "
            "They belong in frontend/demo-build-flag.ts, which is build-time "
            "only. Offenders:\n  " + "\n  ".join(offenders)
        )

    def test_the_built_production_bundle_does_not_contain_the_demo_credentials(self):
        if not DIST.is_dir():
            pytest.skip(
                "frontend/dist has not been built. Run `cd frontend && npm run "
                "build` to exercise the built-bundle check."
            )

        offenders = []
        for path in _built_assets():
            if not path.is_file():
                continue
            text = _read(path)
            for needle in FORBIDDEN:
                if needle in text:
                    offenders.append(f"{path.relative_to(REPO_ROOT)} -> {needle}")

        assert not offenders, (
            "the production bundle contains the seeded demo credentials. "
            "A visitor can read them out of the public JavaScript without "
            "clicking anything. Offenders:\n  " + "\n  ".join(offenders)
        )

    def test_the_demo_helper_is_off_in_a_default_build(self):
        """The default build must not ask for the demo helper.

        frontend/.env.production is the file a production build reads. If it
        ever sets VITE_SHOW_DEMO, the credentials are compiled in again.
        """
        env_production = FRONTEND / ".env.production"
        if not env_production.is_file():
            pytest.skip("frontend/.env.production is not present")

        for line in _read(env_production).splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            name, _, value = stripped.partition("=")
            if name.strip() == "VITE_SHOW_DEMO":
                assert value.strip().lower() in {"", "false", "0", "no", "off"}, (
                    "frontend/.env.production enables VITE_SHOW_DEMO, which "
                    "compiles the seeded demo credentials into the production "
                    "bundle."
                )

    def test_a_local_env_override_cannot_reach_the_production_build(self):
        """A developer's local env file must not reach the production build.

        frontend/Dockerfile.prod does `COPY . .`, so whatever the
        .dockerignore does not exclude is copied into the image and read by
        vite at build time. In a production build vite reads `.env`,
        `.env.production`, `.env.local` and `.env.production.local`; of those
        only `.env.production` is tracked, and the check above only inspects
        `.env.production`. So a local file that sets VITE_SHOW_DEMO would
        compile the seeded demo credentials into the production bundle -- the
        exact thing this file exists to prevent.
        """
        dockerignore = FRONTEND / ".dockerignore"
        assert dockerignore.is_file(), "frontend/.dockerignore is missing"

        patterns = {
            line.strip()
            for line in _read(dockerignore).splitlines()
            if line.strip() and not line.strip().startswith("#")
        }

        for required in (".env", ".env.local", ".env.*.local"):
            assert required in patterns, (
                f"frontend/.dockerignore must exclude {required!r}. "
                "frontend/Dockerfile.prod does `COPY . .`, so a developer's "
                "local env file would be copied into the image and read by "
                "vite at build time -- it could set VITE_SHOW_DEMO and compile "
                "the seeded demo credentials into the production bundle. "
                f"Patterns present: {sorted(patterns)}"
            )

        # frontend/.env.production IS needed by the build: it holds the public
        # VITE_* values. A blanket exclusion would break the image build, so
        # the patterns must stay narrow.
        assert ".env.production" not in patterns, (
            "frontend/.dockerignore excludes frontend/.env.production, which "
            "the build needs (it holds the public VITE_* values). Exclude the "
            "LOCAL files only: .env, .env.local and .env.*.local."
        )

        # A local file that exists right now must not be one the build reads.
        for local in (FRONTEND / ".env", FRONTEND / ".env.local"):
            if local.is_file():
                assert ".env" in patterns, (
                    f"{local.relative_to(REPO_ROOT)} exists and the build "
                    "context would copy it into the image."
                )


class TestTheBuildContextStillContainsWhatTheConfigImports:
    """The production image is built with `context: ./frontend`.

    docker-compose.yml builds the frontend image from the frontend directory
    alone, so a module OUTSIDE frontend/ is not in the build: the image build
    would fail on an import it cannot resolve. The bundle test above builds
    in the checkout, where the whole repository is present, so it CANNOT see
    that class of break -- this test does.
    """

    @pytest.mark.parametrize("config_name", ["vite.config.ts", "vitest.config.ts"])
    def test_every_import_of_the_config_stays_inside_the_build_context(
        self, config_name
    ):
        source = (FRONTEND / config_name).read_text(encoding="utf-8")

        for match in re.finditer(r'from\s+"(\.[^"]+)"', source):
            relative = match.group(1)
            resolved = (FRONTEND / relative).resolve()
            context_root = FRONTEND.resolve()

            assert str(resolved).startswith(str(context_root)), (
                f"{config_name} imports {relative!r}, which is outside the "
                "frontend build context (docker-compose.yml builds the "
                "frontend image with `context: ./frontend`), so "
                "`docker compose build frontend` would fail"
            )

            target = resolved if resolved.suffix else resolved.with_suffix(".ts")
            assert target.exists(), (
                f"{config_name} imports {relative!r}, which does not exist at "
                f"{target}"
            )


class TestTheCredentialsAreNotInTheServedTree:
    """The plaintext must not sit anywhere under frontend/.

    docker-compose.yml bind-mounts the WHOLE frontend/ directory read-only
    at /srv/frontend for Caddy, so anything with the demo login in it is
    one handler edit away from being published. The credentials used to be
    literals in frontend/demo-build-flag.ts, which is inside that
    directory.

    They come from the BUILD ENVIRONMENT now
    (VITE_DEMO_USERNAME / VITE_DEMO_PASSWORD), so no file under
    frontend/ may hold them at all.
    """

    def test_no_file_under_the_frontend_build_context_holds_the_credentials(self):
        offenders = []
        for path in _frontend_tree_files():
            if not path.is_file():
                continue
            if _is_test_file(path):
                continue
            text = _read(path)
            for needle in FORBIDDEN:
                if needle in text:
                    offenders.append(f"{path.relative_to(REPO_ROOT)} -> {needle}")

        assert not offenders, (
            "the seeded demo credentials are in a file under frontend/, and "
            "the whole frontend/ directory is bind-mounted read-only into "
            "the Caddy container that serves the public site. The "
            "credentials belong in the BUILD ENVIRONMENT "
            f"({DEMO_USERNAME_ENV} / {DEMO_PASSWORD_ENV}), not in the "
            "source tree. Offenders:\n  " + "\n  ".join(offenders)
        )


class TestTheBundleCheckIsNotVacuous:
    """The bundle check is only worth something if it CAN fail.

    These two tests build the frontend for real, twice: once the way a
    production build is made, and once with the demo helper explicitly
    asked for. The first must not contain the credentials; the second must.
    """

    def test_a_build_without_the_demo_flag_ships_no_demo_credentials(
        self, production_builds
    ):
        found, files = _needles_found(production_builds["without"], FORBIDDEN)

        assert not found, (
            "a production build without VITE_SHOW_DEMO contains the seeded "
            "demo credentials. A visitor can read them out of the public "
            "JavaScript without clicking anything. Files: " + ", ".join(files)
        )

    def test_a_build_with_the_demo_flag_ships_them_so_the_check_can_fail(
        self, production_builds
    ):
        """The negative control.

        If the flag-on build did NOT contain the credentials, then the
        flag-off build not containing them would prove nothing at all: the
        strings could be missing because the mechanism is broken, or because
        the grep never matches.
        """
        found, files = _needles_found(production_builds["with"], FORBIDDEN)

        assert found == set(FORBIDDEN), (
            "a build WITH the demo helper opted in did not contain both "
            "seeded demo credentials, so the check above cannot fail and "
            f"proves nothing. Found {len(found)} of {len(FORBIDDEN)} in "
            f"{len(files)} file(s)."
        )

    def test_a_stray_env_var_cannot_enable_the_demo_helper(self, production_builds):
        """The opt-in comes from the env FILE, never from process.env.

        Vite's `loadEnv()` merges `process.env` over the .env files, so a
        stray VITE_SHOW_DEMO=true in the build environment -- a CI
        variable, a shell export, a compose build arg -- used to be enough
        to compile the seeded demo credentials into the bundle. The
        .dockerignore fix only covers FILES, so it could not stop that.

        This build carries the credentials AND VITE_SHOW_DEMO=true in its
        environment, and no env file opts in. It must ship neither string.
        """
        found, files = _needles_found(production_builds["stray"], FORBIDDEN)

        assert not found, (
            "a build whose ENVIRONMENT said VITE_SHOW_DEMO=true, but whose "
            "env FILE did not opt in, shipped the seeded demo credentials. "
            "The opt-in must come from the env file only: Vite's loadEnv() "
            "merges process.env over the .env files, so an ambient "
            "variable must not be able to turn the demo helper on. Files: "
            + ", ".join(files)
        )
