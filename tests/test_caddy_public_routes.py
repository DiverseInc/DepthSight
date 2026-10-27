# tests/test_caddy_public_routes.py
"""Caddy must proxy the two root-mounted API routes it was missing.

THE DEFECT THIS PINS
--------------------
`api/routes/public.py` registers its router with NO path prefix and
`api/routes/registry.py` mounts it directly on the app, so two of its
endpoints are served at the ROOT:

    GET /og-image/{public_slug}.png  -> the per-report Open Graph image
    GET /r/{ref_code}                -> the affiliate click counter

caddy.config proxied only /api/*, /render-shared/*, /docs, /openapi.json,
/ws*, /pwa/* and the static /welcome. Both root paths therefore fell through
to the catch-all `handle { reverse_proxy frontend:80 }` and returned the
2.5 KB React app shell instead of the real response: the shared report's
og:image tag pointed at an HTML page rather than a PNG, and the affiliate
click counter in public.py was never reached.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CADDY_CONFIG = REPO_ROOT / "caddy.config"


@pytest.fixture(scope="module")
def config() -> str:
    return CADDY_CONFIG.read_text(encoding="utf-8")


def _handle_body(config: str, path_pattern: str) -> str:
    """The body of the `handle <path_pattern> { ... }` block."""
    match = re.search(
        r"(?m)^\s*handle\s+" + re.escape(path_pattern) + r"\s*\{(?P<body>[^}]*)\}",
        config,
    )
    assert match, f"caddy.config has no `handle {path_pattern} {{ ... }}` block"
    return match.group("body")


def _static_og_image_paths(config: str) -> list:
    """The exact filenames the STATIC og-image handler serves."""
    match = re.search(r"(?m)^\s*@ogimage\s+path\s+(?P<paths>[^\n]+)", config)
    assert match, "caddy.config has no @ogimage path matcher"
    return re.findall(r"(/og-image\.[a-z]+)", match.group("paths"))


class TestTheMissingRootRoutesAreProxied:
    def test_the_dynamic_og_image_route_is_proxied_to_the_api(self, config):
        body = _handle_body(config, "/og-image/*")
        assert "reverse_proxy api:8000" in body, (
            "/og-image/* must be proxied to the API, which serves the "
            "per-report Open Graph image at the root"
        )

    def test_the_affiliate_redirect_route_is_proxied_to_the_api(self, config):
        body = _handle_body(config, "/r/*")
        assert "reverse_proxy api:8000" in body, (
            "/r/* must be proxied to the API, which counts the affiliate "
            "click before redirecting to /register"
        )

    def test_the_api_really_serves_both_paths_at_the_root(self):
        """The proxy target must be right, or the routes are still wrong.

        The served paths are read from the OpenAPI schema rather than
        `app.routes`: FastAPI 0.162 mounts included routers lazily (as
        `_IncludedRouter` entries that carry no `.path`), so the flat route
        list does not show the nested paths.
        """
        from api.depthsight_api import app

        paths = set(app.openapi().get("paths", {}))

        assert "/og-image/{public_slug}.png" in paths, (
            "the API no longer serves the per-report og:image at the root"
        )
        assert "/r/{ref_code}" in paths, (
            "the API no longer serves the affiliate redirect at the root"
        )

    def test_the_public_router_is_mounted_without_a_prefix(self):
        """A prefix would move both routes off the root and break the proxy."""
        from api.routes.public import public_router

        assert public_router.prefix == "", (
            "public_router must keep an empty prefix: /og-image/{slug}.png "
            "and /r/{ref_code} are served at the ROOT"
        )
        router_paths = {getattr(r, "path", None) for r in public_router.routes}
        assert "/og-image/{public_slug}.png" in router_paths
        assert "/r/{ref_code}" in router_paths


class TestTheTwoOgImageHandlersDoNotCollide:
    def test_the_static_handler_still_serves_the_exact_filenames(self, config):
        assert _static_og_image_paths(config) == [
            "/og-image.png",
            "/og-image.jpg",
            "/og-image.jpeg",
        ]

    def test_the_static_matcher_contains_no_wildcard(self, config):
        """A wildcard in @ogimage would swallow /og-image/{slug}.png."""
        match = re.search(r"(?m)^\s*@ogimage\s+path\s+(?P<paths>[^\n]+)", config)
        assert match
        assert "*" not in match.group("paths"), (
            "@ogimage must list exact filenames only; a wildcard there would "
            "shadow the dynamic /og-image/{slug}.png route"
        )

    def test_no_static_path_is_matched_by_the_dynamic_route(self, config):
        """The two sets must be disjoint, whichever order Caddy evaluates."""
        for path in _static_og_image_paths(config):
            assert not path.startswith("/og-image/"), (
                f"{path} would be matched by the dynamic /og-image/* route, "
                "so the static og-image handler would never run"
            )


class TestTheCatchAllStillRunsLast:
    def test_the_spa_catch_all_is_present(self, config):
        assert "reverse_proxy frontend:80" in config

    def test_the_spa_catch_all_is_the_last_handle_block(self, config):
        """Every other handle must win before the SPA catch-all.

        The catch-all proxies EVERY path to the React app, so a route added
        after it would never be reached.
        """
        handles = re.findall(r"(?m)^\s*handle\s*([^\s{]*)\s*\{", config)
        assert handles, "caddy.config has no handle blocks"
        assert handles[-1] == "", (
            f"the SPA catch-all is not the last handle block (last is "
            f"{handles[-1]!r}); a route placed after it would be unreachable"
        )
        assert "reverse_proxy frontend:80" in _handle_body(config, "")
