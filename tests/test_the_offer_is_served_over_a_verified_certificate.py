# tests/test_the_offer_is_served_over_a_verified_certificate.py
"""The AGPL source offer must be served over a certificate that VERIFIES.

THE DEFECT THIS PINS
--------------------
The deploy gate and the smoke test both decide "is the source offer live?"
with `curl -sk`. The `-k` flag tells curl to skip certificate verification,
so neither check can tell the real offer from one served through an
impersonated or broken certificate: the answer is the same either way.

That is the wrong question being asked in the wrong place. The liveness
checks ("is the server answering?") are a container-health question, and
skipping verification there is defensible -- a certificate problem should
not make a healthy site look dead. But the OFFER check is a public-facing
compliance check: the offer is a document served to strangers, and "the
offer is live" has to mean "the offer is served properly".

THE MEASUREMENT BEHIND THIS
---------------------------
The live certificate was checked directly (2026-11-09): a valid Let's Encrypt
certificate for `depthsight.diverseinc.net`, CN and SAN matching the
hostname, notAfter 2027-01-12, TLS 1.3, and `ssl.create_default_context()`
verification SUCCEEDS. So a verifying curl works against this host today --
the `-k` was never needed.

And the ops sidecar needs a CA bundle to verify at all: its image is
`alpine:3.19` with `apk add curl`, so `ca-certificates` is installed
explicitly rather than assumed.

TWO HOLES THIS FILE ALREADY HAD, FOUND BY MUTATION
---------------------------------------------------
The first version of the two checks below was too weak, and the mutation
harness proved it -- two mutants survived it:

  * M4 neutered the deploy gate's certificate check (`if ! curl ...` became
    `if false; then`), and the old assertion still passed, because it only
    looked NEAR the failure message for *some* verifying curl -- and the
    neighbouring function had one. The check is now required to BE an
    `if ! curl ... /source` construct, so a check that asks nothing cannot
    pass.
  * M5 turned the `ca-certificates` line in the ops Dockerfile into a
    COMMENT, and the old assertion -- a substring search for
    "ca-certificates" -- still passed. The assertion now reads the package
    list the image actually installs, and a package that is only mentioned
    in a comment is not installed.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_TEST = REPO_ROOT / "ops" / "scripts" / "smoke-test.sh"
UPDATE_SH = REPO_ROOT / "update.sh"
OPS_DOCKERFILE = REPO_ROOT / "ops" / "Dockerfile"


def _tokens(args: str) -> list:
    return re.findall(r'"[^"]*"|\'[^\']*\'|\S+', args)


def _url_of(tokens: list):
    """The URL a curl invocation points at.

    A curl line carries several path-looking tokens (`-o /dev/null`,
    `2>/dev/null`), so the URL is the candidate that names the path being
    asked for, and only then the last candidate.
    """
    candidates = [t.strip("\"'") for t in tokens
                  if t.strip("\"'").startswith("$")
                  or t.strip("\"'").startswith("/")
                  or "://" in t.strip("\"'")]
    for c in reversed(candidates):
        if "/source" in c:
            return c
    return candidates[-1] if candidates else None


def _flags_of(tokens: list) -> list:
    return [t for t in tokens if t.startswith("-")]


def skips_verification(flags) -> bool:
    """Does this flag set tell curl to skip certificate verification?

    `-k` is the short form and `--insecure` the long one, but they COMBINE:
    `-sk` means "silent" plus "insecure" and skips verification just the same.
    A check that only looked for the exact token `-k` would pass a file that
    says `-sk` -- so the short flags are read as letters.
    """
    for f in flags:
        if f.startswith("--"):
            if f == "--insecure":
                return True
        elif "k" in f[1:]:
            return True
    return False


def curl_invocations(text: str) -> list:
    """Each curl invocation in a shell file, as (flags, url).

    Line continuations are joined first, so a curl split over two lines is
    one invocation.
    """
    joined = re.sub(r"\\\r?\n", " ", text)
    out = []
    for m in re.finditer(r"curl\s+([^|;\n]*)", joined):
        tokens = _tokens(m.group(1))
        out.append((_flags_of(tokens), _url_of(tokens)))
    return out


def certificate_checks(text: str) -> list:
    """Each `if ! curl ...` construct, as (flags, url).

    A certificate check is not "a curl somewhere near the message" -- it is
    a request that is EXPECTED TO FAIL, and whose failure is acted on. So
    the construct itself is what is pinned: if the check stops being an
    `if ! curl`, it can never fire.
    """
    out = []
    for m in re.finditer(r"if\s+!\s*curl\s+([^|;\n]*)", text):
        tokens = _tokens(m.group(1))
        out.append((_flags_of(tokens), _url_of(tokens)))
    return out


def offer_checks(text: str) -> list:
    return [(f, u) for f, u in curl_invocations(text) if u and "/source" in u]


def test_the_files_exist():
    for p in (SMOKE_TEST, UPDATE_SH, OPS_DOCKERFILE):
        assert p.is_file(), f"{p} is missing"


@pytest.mark.parametrize("path", [SMOKE_TEST, UPDATE_SH], ids=["smoke-test", "update"])
def test_the_offer_check_verifies_the_certificate(path):
    checks = offer_checks(path.read_text(encoding="utf-8"))
    assert checks, (
        f"{path.name} has no curl against /source, so the offer's liveness is "
        "not checked at all"
    )
    for flags, url in checks:
        assert not skips_verification(flags), (
            f"{path.name}: the offer check against {url} skips certificate "
            "verification, so it cannot tell the real offer from one served "
            "through an impersonated certificate"
        )


def test_the_deploy_gate_reports_a_certificate_failure_as_itself():
    """A broken certificate must not be reported as 'the offer is missing'.

    The operator who is told the offer is missing will restart caddy and
    look at the route. The operator who is told the certificate did not
    verify will fix the certificate. Conflating the two sends them the wrong
    way.
    """
    text = UPDATE_SH.read_text(encoding="utf-8")
    assert "did not verify" in text, (
        "update.sh does not report a certificate failure distinctly"
    )
    idx = text.index("did not verify")
    assert "exit 1" in text[idx: idx + 1200], (
        "update.sh mentions the certificate but does not stop the deploy when "
        "the certificate does not verify"
    )


def test_the_deploy_gate_actually_asks_the_certificate():
    """The check must BE a request that is expected to fail.

    A block that mentions the certificate but never asks anything reports
    nothing and stops nothing, and a check that asks the wrong URL checks
    the wrong thing.
    """
    text = UPDATE_SH.read_text(encoding="utf-8")
    checks = [(f, u) for f, u in certificate_checks(text) if u and "/source" in u]
    assert checks, (
        "update.sh reports a certificate failure but never makes the request "
        "that could fail: the check must be an `if ! curl ... /source` "
        "construct, so that a check which asks nothing cannot pass"
    )
    for flags, url in checks:
        assert not skips_verification(flags), (
            f"update.sh: the certificate check against {url} skips certificate "
            "verification, so it can never fail"
        )


def _apk_packages(text: str) -> list:
    """The packages the ops image actually INSTALLS.

    A package named only in a comment is not installed, and a package on a
    line that is commented out is not installed either -- so the comment
    lines are dropped before the list is read.
    """
    out = []
    inside = False
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if not inside:
            if "apk add" not in s:
                continue
            inside = True
            s = s.split("apk add", 1)[1]
        s = s.replace("\\", " ").split("&&")[0]
        for tok in s.split():
            if tok in ("apk", "add", "RUN", "--no-cache") or tok.startswith("-"):
                continue
            out.append(tok)
        if "&&" in raw:
            inside = False
    return out


def test_the_ops_image_can_verify_a_certificate():
    """The smoke test runs in the ops sidecar. Alpine + curl does not carry
    a CA bundle unless one is installed, so it is installed explicitly."""
    packages = _apk_packages(OPS_DOCKERFILE.read_text(encoding="utf-8"))
    assert packages, (
        "ops/Dockerfile installs no packages, so the smoke test cannot verify "
        "a certificate even if it tries to"
    )
    assert "ca-certificates" in packages, (
        f"ops/Dockerfile does not install ca-certificates (it installs "
        f"{packages}), so the smoke test cannot verify a certificate even if "
        "it tries to"
    )


def test_the_liveness_checks_are_not_mistaken_for_the_offer_check():
    """The offer check and the liveness check are different questions.

    This pins that the offer check is the one that verifies, so that a
    future edit cannot quietly move the verification onto a check that is
    about something else.
    """
    text = SMOKE_TEST.read_text(encoding="utf-8")
    assert "/source" in text
    checks = offer_checks(text)
    assert checks
    # every /source check verifies (covered above); this asserts the file
    # still checks the offer's BODY, not only its status code
    assert "grep -q \"DiverseInc/DepthSight\"" in text or \
        "grep -q 'DiverseInc/DepthSight'" in text, (
        "the smoke test must check the offer's BODY: a 200 on /source is also "
        "what the React SPA catch-all answers when the route is missing"
    )
