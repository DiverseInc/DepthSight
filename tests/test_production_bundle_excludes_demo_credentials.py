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

To run the full check:

    cd frontend && npm run build && cd ..
    python -m pytest tests/test_production_bundle_excludes_demo_credentials.py
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = REPO_ROOT / "frontend"
SRC = FRONTEND / "src"
DIST = FRONTEND / "dist"

# The seeded demo account created by scripts/seed_demo_data.py.
FORBIDDEN = ("DemoPassword123", "alex_trader")

# Files that end up in the bundle: the app sources, the Vite entry template
# and everything copied verbatim out of public/.
TEXT_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".css", ".html", ".json"}


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


def _built_assets():
    yield from sorted(DIST.rglob("*"))


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
