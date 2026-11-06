# tests/test_public_source_offer.py
"""The AGPL-3.0 section 13 source offer must exist, and must keep existing.

THE DEFECT THIS PINS
--------------------
DepthSight is licensed under the GNU Affero General Public License v3.0
(LICENSE). Section 13 of that licence requires that every user who
interacts with the modified program over a network is offered the
corresponding source code of the MODIFIED version:

    "Notwithstanding any other provision of this License, if you modify the
     Program, your modified version must prominently offer all users
     interacting with it remotely through a computer network (if your version
     supports such interaction) an opportunity to receive the Corresponding
     Source of your version by providing access to the Corresponding Source
     from a network server at no charge, through some standard or customary
     means of facilitating copying of software. [...]"

The hosted service at depthsight.diverseinc.net is that modified program.
Before this test there was no source offer anywhere in the repository: no
page, no route, no statement of the repository address, and no way for a
user to ask for the source by email. The offer also has to SURVIVE --
the page is a static file in the bind-mounted frontend/ tree, so it can be
edited away or the Caddy route that serves it can be dropped, and nothing
else in the repository would notice.

So this file pins the offer as a whole:

  * the page exists and is served at /source by the same mechanism as
    /welcome (a static file rewritten to exactly one path, so the rest of
    the bind-mounted frontend/ tree can never be served);
  * it names the licence, offers the source to everyone who interacts with
    the service, free of charge, for as long as the service runs;
  * it carries the repository address and a download link that works
    without a GitHub account;
  * it says which source code corresponds to the version the reader is
    running;
  * it attributes the upstream project, its licence and its CLA;
  * it states the email route by which the source can be requested;
  * it says the offer covers the running service INCLUDING our
    modifications;
  * the welcome page links to it, outside the pricing section.

Every assertion below reads the committed files, so removing
frontend/source.html, or editing one of these statements out of it, turns
this file red.
"""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_HTML = REPO_ROOT / "frontend" / "source.html"
WELCOME_HTML = REPO_ROOT / "frontend" / "welcome.html"
CADDY_CONFIG = REPO_ROOT / "caddy.config"
FRONTEND_ENV = REPO_ROOT / "frontend" / ".env.production"

# The public address of the offer.
SOURCE_PATH = "/source"
REPOSITORY_URL = "https://github.com/DiverseInc/DepthSight"
ARCHIVE_URL = "https://github.com/DiverseInc/DepthSight/archive/refs/heads/main.tar.gz"
UPSTREAM_URL = "https://github.com/DepthSight-Pro/DepthSight"
STATUS_URL = "https://depthsight.diverseinc.net/api/v1/status"
OPENAPI_URL = "https://depthsight.diverseinc.net/openapi.json"


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

def _norm(text: str) -> str:
    """Collapse whitespace so a sentence can be matched across line breaks."""
    return " ".join(text.split())


class _PageParser(HTMLParser):
    """The visible text of a page, and the hrefs it links to."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list = []
        self.hrefs: list = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.chunks.append(data)

    @property
    def text(self) -> str:
        return _norm(" ".join(self.chunks))


@pytest.fixture(scope="module")
def page_html() -> str:
    """The raw markup of the source offer page."""
    assert SOURCE_HTML.is_file(), (
        "frontend/source.html must exist: it is the section 13 source offer "
        "for the running service, and the only thing Caddy serves at /source"
    )
    return SOURCE_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def page_text(page_html: str) -> str:
    """The visible text of the source offer page."""
    parser = _PageParser()
    parser.feed(page_html)
    return parser.text


@pytest.fixture(scope="module")
def page_hrefs(page_html: str) -> list:
    parser = _PageParser()
    parser.feed(page_html)
    return parser.hrefs


@pytest.fixture(scope="module")
def welcome_html() -> str:
    return WELCOME_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def caddy_config() -> str:
    return CADDY_CONFIG.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def pricing_section(welcome_html: str) -> str:
    """The pricing block of the welcome page.

    The pricing copy is owner-approved wording; the source offer must not
    be added to it, so it is extracted here to prove the link is elsewhere.
    """
    match = re.search(
        r"<section id=\"pricing\">.*?</section>", welcome_html, flags=re.S
    )
    assert match, "the welcome page has no <section id=\"pricing\">"
    return match.group(0)


# --------------------------------------------------------------------------
# The page itself
# --------------------------------------------------------------------------

class TestTheOfferPageExists:
    def test_the_page_is_committed_to_the_repository(self, page_html: str):
        assert page_html.strip(), "frontend/source.html is empty"
        assert "<html" in page_html and "</html>" in page_html, (
            "frontend/source.html must be a complete HTML document"
        )

    def test_the_page_is_served_at_the_public_source_path(self, caddy_config: str):
        """Caddy must route /source to the page.

        The mechanism is the one /welcome already uses: a matcher on the
        public path, then a rewrite to exactly one file in the bind-mounted
        frontend/ tree.
        """
        match = re.search(
            r"(?m)^\s*@source\s+path\s+(?P<paths>[^\n]+)", caddy_config
        )
        assert match, "caddy.config has no @source path matcher"
        paths = match.group("paths").split()
        assert SOURCE_PATH in paths, (
            f"the @source matcher must cover {SOURCE_PATH}; it covers {paths}"
        )

    def test_the_source_handler_can_only_serve_source_html(self, caddy_config: str):
        """The handler must pin ONE file, so the served tree stays private.

        docker-compose.yml bind-mounts the whole frontend/ directory at
        /srv/frontend so that Caddy can serve frontend/welcome.html. That
        directory holds the entire frontend source tree, so a file_server
        that could reach an arbitrary path under it would publish the
        source. The rewrite to exactly one path is what prevents that.
        """
        body = re.search(
            r"(?m)^\s*handle\s+@source\s*\{(?P<body>[^}]*)\}", caddy_config
        )
        assert body, "caddy.config has no `handle @source { ... }` block"
        assert "rewrite * /source.html" in body.group("body"), (
            "the @source handler must rewrite every request to /source.html, "
            "so it cannot serve another file from the mounted frontend/ "
            "directory"
        )

    def test_the_source_handler_is_not_after_the_spa_catch_all(self, caddy_config: str):
        """A route after the catch-all is never reached.

        The catch-all proxies EVERY path to the React app, so /source would
        silently return the SPA shell instead of the offer.
        """
        handles = re.findall(r"(?m)^\s*handle\s*([^\s{]*)\s*\{", caddy_config)
        assert handles, "caddy.config has no handle blocks"
        assert handles[-1] == "", (
            f"the SPA catch-all is not the last handle block (last is "
            f"{handles[-1]!r}); a route placed after it would be unreachable"
        )
        assert "@source" in handles, "caddy.config has no `handle @source` block"


# --------------------------------------------------------------------------
# What the licence requires the offer to say
# --------------------------------------------------------------------------

class TestTheOfferSaysWhatTheLicenceRequires:
    def test_the_page_names_the_licence(self, page_text: str):
        assert "GNU Affero General Public License v3.0" in page_text, (
            "the offer must name the licence the source is offered under"
        )
        assert "AGPL-3.0" in page_text, (
            "the offer must name the licence the source is offered under"
        )

    def test_the_page_cites_section_13(self, page_text: str):
        assert "section 13" in page_text.lower(), (
            "the offer must say that this is the section 13 source offer"
        )

    def test_the_offer_is_made_to_everyone_who_interacts_with_the_service(
        self, page_text: str
    ):
        """Section 13 is about users interacting REMOTELY, so the offer
        cannot be limited to account holders or to paying customers."""
        lowered = page_text.lower()
        assert "every person who interacts with this service" in lowered, (
            "the offer must be made to every person who interacts with the "
            "service, not only to account holders"
        )
        assert "whether or not you have an account" in lowered, (
            "the offer must not be conditional on having an account"
        )

    def test_the_source_is_offered_free_of_charge(self, page_text: str):
        assert "free of charge" in page_text.lower(), (
            "section 13 requires the source to be offered at no charge"
        )

    def test_the_offer_covers_the_running_service_including_our_modifications(
        self, page_text: str
    ):
        """The offer must be of the MODIFIED program, not of upstream."""
        lowered = page_text.lower()
        assert "the source code of the running service" in lowered, (
            "the offer must be of the source of the running service"
        )
        assert "including the changes we have made" in lowered, (
            "the offer must state that it includes our modifications"
        )
        assert "what \"our modifications\" means" in lowered, (
            "the page must say what our modifications means"
        )

    def test_the_offer_holds_for_as_long_as_the_service_runs(self, page_text: str):
        assert "for as long as the service is running" in page_text.lower(), (
            "the offer must say it is open for as long as the service runs"
        )


# --------------------------------------------------------------------------
# Where to get the source
# --------------------------------------------------------------------------

class TestTheOfferGivesTheSource:
    def test_the_repository_address_is_on_the_page(self, page_html: str, page_hrefs: list):
        assert REPOSITORY_URL in page_html, (
            "the offer must carry the repository address in full"
        )
        assert REPOSITORY_URL in page_hrefs, (
            "the repository address must be a link the reader can click"
        )

    def test_the_download_link_needs_no_github_account(self, page_html: str, page_text: str, page_hrefs: list):
        """The archive endpoint serves the tree without authentication."""
        assert ARCHIVE_URL in page_hrefs, (
            "the offer must link an archive that can be downloaded without a "
            "GitHub account"
        )
        assert re.search(r"without a github account", page_text, re.I), (
            "the offer must say that no GitHub account is needed"
        )

    def test_the_offer_says_that_no_separate_release_package_is_published(
        self, page_text: str
    ):
        """Stated plainly, because there is no release asset to point at."""
        assert "we do not publish a separate release package" in page_text.lower(), (
            "the offer must say plainly that no separate release package of "
            "the hosted build is published"
        )

    def test_the_page_says_which_source_matches_the_running_version(
        self, page_text: str, page_hrefs: list
    ):
        lowered = page_text.lower()
        assert "which source code matches the version you are running" in lowered, (
            "the offer must say which source code corresponds to the "
            "version the reader is running"
        )
        assert STATUS_URL in page_hrefs, (
            "the offer must point at the status endpoint that reports the "
            "running version"
        )
        assert OPENAPI_URL in page_hrefs, (
            "the offer must point at the API description that reports the "
            "running version"
        )
        assert "version" in lowered and "commit" in lowered, (
            "the offer must be honest that the version identifies the "
            "release line rather than one commit"
        )


# --------------------------------------------------------------------------
# Upstream attribution
# --------------------------------------------------------------------------

class TestTheOfferNamesTheUpstream:
    def test_the_upstream_project_is_named(self, page_html: str, page_hrefs: list):
        assert UPSTREAM_URL in page_html, (
            "the offer must attribute the upstream project by its address"
        )
        assert UPSTREAM_URL in page_hrefs, (
            "the upstream project must be a link the reader can click"
        )

    def test_the_upstream_licence_is_agpl(self, page_text: str):
        assert "DepthSight-Pro/DepthSight" in page_text, (
            "the offer must name the upstream project in words as well as by "
            "address"
        )

    def test_the_fork_relationship_is_stated(self, page_text: str):
        lowered = page_text.lower()
        assert "fork" in lowered and "upstream" in lowered, (
            "the offer must state the relationship between this service and "
            "the upstream project"
        )

    def test_the_cla_is_attributed_factually(self, page_text: str):
        """The CLA is upstream's, and it applies to upstream contributions."""
        lowered = page_text.lower()
        assert "contributor license agreement" in lowered, (
            "the offer must mention the Contributor License Agreement"
        )
        assert "cla" in lowered, (
            "the offer must name the CLA"
        )
        assert "contributions made to the upstream project" in lowered, (
            "the offer must say that the CLA applies to contributions made "
            "to the upstream project"
        )
        assert "royalty-free" in lowered, (
            "the offer must state what signing the CLA grants"
        )

    def test_the_offer_does_not_claim_the_cla_governs_this_offer(self, page_text: str):
        """The CLA must not be used to narrow the section 13 offer."""
        assert "the cla does not change the terms on which we offer" in (
            page_text.lower()
        ), (
            "the offer must say that the CLA does not change the terms of "
            "the source offer"
        )


# --------------------------------------------------------------------------
# The email route
# --------------------------------------------------------------------------

class TestTheSourceCanBeRequestedByEmail:
    def test_the_mail_address_is_on_the_page(self, page_hrefs: list):
        mailtos = [h for h in page_hrefs if h.startswith("mailto:")]
        assert mailtos, "the offer must give a mailto: address to request the source"
        assert any(h.startswith("mailto:support@diverseinc.net") for h in mailtos), (
            f"the offer must offer a request route by email; it offers {mailtos}"
        )

    def test_the_request_route_carries_a_subject_line(self, page_hrefs: list):
        subjects = [h for h in page_hrefs if "subject=" in h]
        assert subjects, "the request route must pre-fill a subject line"
        assert any("AGPL source request" in h.replace("%20", " ") for h in subjects), (
            f"the pre-filled subject must name the request; it is {subjects}"
        )

    def test_the_address_is_the_one_the_service_uses(self):
        """One support inbox, not a second address nobody reads."""
        env = FRONTEND_ENV.read_text(encoding="utf-8")
        match = re.search(r"(?m)^VITE_SUPPORT_EMAIL=(?P<email>\S+)\s*$", env)
        assert match, "frontend/.env.production has no VITE_SUPPORT_EMAIL"
        assert match.group("email") == "support@diverseinc.net", (
            "the source offer must use the support address the running "
            f"service already uses; it is {match.group('email')!r}"
        )


# --------------------------------------------------------------------------
# Discoverability
# --------------------------------------------------------------------------

class TestTheOfferIsDiscoverable:
    def test_the_welcome_page_links_to_the_offer(self, welcome_html: str):
        assert f'href="{SOURCE_PATH}"' in welcome_html, (
            f"the welcome page must link to the source offer at {SOURCE_PATH}"
        )

    def test_the_link_is_in_the_navigation_or_the_footer(self, welcome_html: str):
        nav = re.search(r"<nav\b.*?</nav>", welcome_html, flags=re.S)
        footer = re.search(r"<footer\b.*?</footer>", welcome_html, flags=re.S)
        assert nav or footer, "the welcome page has no nav or footer to link from"
        blocks = [b.group(0) for b in (nav, footer) if b]
        assert any(f'href="{SOURCE_PATH}"' in block for block in blocks), (
            f"the link to the source offer must be in the navigation or the "
            f"footer of the welcome page, not only somewhere else"
        )

    def test_the_link_is_not_in_the_pricing_section(
        self, welcome_html: str, pricing_section: str
    ):
        """The pricing copy is owner-approved wording and is not the place
        for a licence notice; the offer must be linked from elsewhere."""
        assert f'href="{SOURCE_PATH}"' not in pricing_section, (
            "the source offer link must not be added to the pricing section"
        )

    def test_the_pricing_section_is_untouched_by_the_offer(self, pricing_section: str):
        """The pricing copy is owner-approved wording and is not edited here.

        It already said "Source available under AGPL v3." before the offer
        existed, and that sentence is left exactly as it was: the offer is
        linked from the navigation and the footer instead, so nothing in
        the pricing block moves.
        """
        normalised = _norm(pricing_section)
        assert "Source available under AGPL v3." in normalised, (
            "the pricing fineprint is owner-approved wording and must be "
            "left exactly as it is"
        )
        assert 'href="/source"' not in pricing_section, (
            "the source offer link must not be added to the pricing section"
        )
        assert "Source code" not in normalised, (
            "the source offer link must not be added to the pricing section"
        )


# --------------------------------------------------------------------------
# The offer must not become a pricing claim
# --------------------------------------------------------------------------

class TestTheOfferClaimsNothingAboutPlans:
    def test_the_page_carries_no_price_or_quota(self, page_text: str):
        """The offer page is a licence notice, not a sales page.

        Prices, quotas and plan names live in api/plans_config.yml and are
        asserted against the pricing page; a second copy of them here would
        be a second, unchecked claim.
        """
        assert "$" not in page_text, "the offer page must not quote a price"
        for word in ("Unlimited", "backtests per day", "per month", "Free plan",
                     "Standard plan", "Professional plan", "upgrade", "cancel"):
            assert word not in page_text, (
                f"the offer page must not make a plan or quota claim ({word!r})"
            )
