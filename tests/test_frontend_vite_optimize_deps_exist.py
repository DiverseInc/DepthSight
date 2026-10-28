"""Every dependency Vite pre-bundles must be a REAL dependency of this project.

MEASURED DEFECT (2026-10-28, review panel round 2 on fix/truthful-public-pricing):
the branch rewrote frontend/vite.config.ts to inject the demo build flag and, in
the rewrite, renamed one entry of `optimizeDeps.include` from
`react-resizable-panels` to `@tanstack/react-resizable-panels`.

That package does not exist in this project. frontend/package.json depends on
`react-resizable-panels` (^4.9.0) and frontend/src/components/ui/resizable.tsx
imports from `react-resizable-panels`. The production BUILD is unaffected --
`optimizeDeps` is a dev-server-only option -- so the defect is invisible to the
build and to the CI, and it breaks `npm run dev`.

Why a guard and not just the one-line fix: the fix restores one string. The
CLASS is "a pre-bundled dependency name that no longer matches the project".
This guard fails on any such name, in either direction:

  (a) the name is not in package.json's dependencies or devDependencies, so it
      cannot be resolved at all; or
  (b) the name IS a real dependency but nothing in frontend/src imports it, so
      it was renamed to something that exists but is never used.

Both are silent: the build passes either way.
"""

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FRONTEND = REPO / "frontend"
SRC = FRONTEND / "src"
VITE_CONFIG = FRONTEND / "vite.config.ts"
PACKAGE_JSON = FRONTEND / "package.json"

# `optimizeDeps: { include: [ "a", "b" ], ... }` -- the array is matched with a
# non-greedy body so a later `include:` in the file cannot be picked up first.
_INCLUDE_RE = re.compile(
    r"optimizeDeps\s*:\s*\{[^}]*?include\s*:\s*\[(?P<body>.*?)\]",
    re.DOTALL,
)
_QUOTED_RE = re.compile(r"""["']([^"']+)["']""")


def _optimize_deps() -> list[str]:
    text = VITE_CONFIG.read_text(encoding="utf-8")
    match = _INCLUDE_RE.search(text)
    assert match is not None, (
        "could not find `optimizeDeps: { include: [...] }` in "
        f"{VITE_CONFIG}. If the vite config was restructured, update this guard "
        "to match the new shape -- do not delete the guard."
    )
    return _QUOTED_RE.findall(match.group("body"))


def _declared_dependencies() -> set[str]:
    data = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    names: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        names.update(data.get(key) or {})
    return names


def _source_files() -> list[Path]:
    if not SRC.is_dir():
        return []
    return [p for p in SRC.rglob("*") if p.suffix in (".ts", ".tsx") and p.is_file()]


def _imported_names(files: list[Path]) -> set[str]:
    imported: set[str] = set()
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in re.finditer(
            r"""(?:from|import|require)\s*\(?\s*["']([^"']+)["']""", text
        ):
            imported.add(match.group(1))
    return imported


@pytest.fixture(scope="module")
def optimize_deps() -> list[str]:
    deps = _optimize_deps()
    # The guard must not pass because it found nothing to check.
    assert deps, (
        "optimizeDeps.include was parsed but is EMPTY -- the guard would pass "
        "vacuously. Fix the parser or the config, not the assertion."
    )
    return deps


def test_every_optimize_dep_is_a_declared_dependency(optimize_deps):
    declared = _declared_dependencies()
    missing = [d for d in optimize_deps if d not in declared]
    assert not missing, (
        "vite optimizeDeps.include names packages that package.json does not "
        f"declare: {missing}. Declared names include: {sorted(declared)}."
    )


def test_every_optimize_dep_is_actually_imported(optimize_deps):
    files = _source_files()
    assert files, (
        f"no .ts/.tsx files found under {SRC} -- the import guard would pass "
        "vacuously."
    )
    imported = _imported_names(files)
    unused = [d for d in optimize_deps if d not in imported]
    assert not unused, (
        "vite optimizeDeps.include pre-bundles packages that nothing in "
        f"frontend/src imports: {unused}. A renamed package that exists but is "
        "never imported is the same silent defect as one that does not exist."
    )


def test_the_guard_detects_a_renamed_package():
    """Control test: the guard must FAIL on a package name that is not declared.

    Without this, a guard that can only pass is indistinguishable from no guard.
    """
    declared = _declared_dependencies()
    assert "react-resizable-panels" in declared, (
        "the control test assumes `react-resizable-panels` is the real "
        "dependency name; if it was renamed for real, update this control."
    )
    renamed = "@tanstack/react-resizable-panels"
    assert renamed not in declared, (
        f"{renamed} is unexpectedly a declared dependency -- the control test "
        "no longer exercises a missing package."
    )
    missing = [d for d in [renamed] if d not in declared]
    assert missing, "the guard's own failure condition did not trigger"
