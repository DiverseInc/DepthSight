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

THE THIRD DEFECT THIS FILE NOW PINS (MEASURED 2026-11-12)
-------------------------------------------------------
The deploy gate's certificate check made ONE request with NO retry, while
the two sibling checks right below it (`source_offer_is_live`,
`site_root_is_live`) both used a bounded three-attempt retry. It was the
only check in update.sh with no retry.

And it reported EVERY failure as "the TLS certificate did not verify".
curl's exit codes are DISTINCT: 60 is a certificate that does not verify,
7 is "connection refused", 28 is "timeout". ONLY 60 is a certificate
failure. So a caddy that was still coming up right after
`docker compose restart caddy` (the script sleeps 3 seconds) turned a
GOOD deploy into a red "certificate did not verify" and `exit 1`, and
sent the operator to fix a certificate that was perfectly fine.

The check is a FUNCTION now -- `verify_offer_certificate` -- because the
REAL function has to be the thing the tests exercise: a copy of the logic
here would prove nothing about the script (AGENTS.md: "Verify against the
real function, not a reimplementation"). It reads curl's exit status in
the ELSE branch of `if curl ...`; inside `if ! curl` the `$?` is the
status of the NEGATION (0) and not of the command, so the negation form
cannot report WHICH failure it was. It retries a BOUNDED three times
with a pause between them, like its two siblings, and picks the message
from the captured status: 60 is the certificate, anything else is the
SITE not answering.
"""

import re
from pathlib import Path

import pytest

from tests.test_update_sh_records_the_deployed_commit import (
    _extract_function,
    _run_bash_raw,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_TEST = REPO_ROOT / "ops" / "scripts" / "smoke-test.sh"
UPDATE_SH = REPO_ROOT / "update.sh"
OPS_DOCKERFILE = REPO_ROOT / "ops" / "Dockerfile"

# The name of the deploy gate's certificate check in update.sh.
CERT_FUNCTION = "verify_offer_certificate"


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
    """Each `if curl ...` construct, as (flags, url).

    A certificate check is not "a curl somewhere near the message" -- it is
    a request that is EXPECTED TO FAIL, and whose failure is acted on. So
    the construct itself is what is pinned: if the check stops being a curl
    that is the CONDITION of an `if`, it can never fire.

    The construct used to be `if ! curl ...`, and it CANNOT be that any
    more: inside `if ! cmd` the `$?` is the status of the NEGATION (0) and
    not of the command, so the exit code of curl -- the thing that tells a
    certificate failure (60) apart from a site that is not answering (7,
    28) -- cannot be read from there. The check now reads the status in
    the `else` branch of a plain `if curl ...; then ... else status=$?;
    fi`.

    The negation is still accepted so that the guard pins the QUESTION
    ("is this a request whose failure is acted on?") and not one spelling
    of it: the old shape would fail a check that verifies nothing just as
    the new one does.
    """
    out = []
    for m in re.finditer(r"if\s+(!\s*)?curl\s+([^|;\n]*)", text):
        tokens = _tokens(m.group(2))
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

    The construct is now `if curl ...; then ... else status=$?; fi`: the
    negation form `if ! curl` CANNOT be used any more, because inside it
    the `$?` is the status of the NEGATION (0) and not of the command, so
    the exit code of curl -- the thing that tells a certificate failure
    (60) apart from a site that is not answering (7, 28) -- could not be
    read at all. The negation is still ACCEPTED by the matcher so that
    the guard pins the QUESTION ("is this a request whose failure is acted
    on?") rather than one spelling of it.
    """
    text = UPDATE_SH.read_text(encoding="utf-8")
    checks = [(f, u) for f, u in certificate_checks(text) if u and "/source" in u]
    assert checks, (
        "update.sh reports a certificate failure but never makes the request "
        "that could fail: the check must be an `if curl ... /source` "
        "construct, so that a check which asks nothing cannot pass"
    )
    for flags, url in checks:
        assert not skips_verification(flags), (
            f"update.sh: the certificate check against {url} skips certificate "
            "verification, so it can never fail"
        )


def test_the_certificate_check_is_the_one_the_deploy_calls():
    """A check that is defined but never CALLED cannot fail a deploy.

    This is STRICTLY STRONGER than the construct guard above. The old
    guard was satisfied by any `if ! curl ... /source` construct in the
    file; the fix moved that construct into a function, and a construct
    alone no longer proves that the gate can go red -- a function that is
    never called, or whose failure does not stop the deploy, would
    satisfy it. So the call path is pinned as well.
    """
    text = UPDATE_SH.read_text(encoding="utf-8")
    function = _extract_function(text, CERT_FUNCTION)
    checks = [
        (f, u) for f, u in certificate_checks(function) if u and "/source" in u
    ]
    assert checks, (
        f"{CERT_FUNCTION}() in update.sh makes no request against /source that "
        "could fail, so the deploy gate can never go red on the certificate"
    )
    for flags, url in checks:
        assert not skips_verification(flags), (
            f"update.sh: the certificate check against {url} skips certificate "
            "verification, so it can never fail"
        )

    call = re.search(
        rf"(?m)^if ! {CERT_FUNCTION}; then$", text
    )
    assert call, (
        f"update.sh never calls {CERT_FUNCTION}() in a condition, so its "
        "failure cannot stop the deploy"
    )
    after = text[call.end(): call.end() + 200]
    assert "exit 1" in after, (
        f"update.sh calls {CERT_FUNCTION}() but does not exit when it fails, "
        "so a broken certificate would be reported and the deploy would carry "
        "on as a success"
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


# --------------------------------------------------------------------------
# The deploy gate's certificate check, RUN
# --------------------------------------------------------------------------
def _run_certificate_check(fail_times: int = 0, fail_status: int = 0) -> dict:
    """The REAL check, lifted out of update.sh and run under bash.

    The stub `curl` COUNTS its calls -- so the retry bound is MEASURED,
    not assumed -- and returns a chosen exit status, so the 60-vs-other
    distinction is measured too. The stub `sleep` returns immediately, so
    the retry test does not spend ten real seconds.

    `fail_times` is how many of the FIRST calls fail; the calls after
    that succeed. `fail_status` is the curl exit code those calls return.
    """
    function = _extract_function(
        UPDATE_SH.read_text(encoding="utf-8"), CERT_FUNCTION
    )
    body = (
        # `set -e` is in force in update.sh (line 7) and is reproduced here,
        # so the check is exercised in the SAME conditions it runs under.
        "set -e\n"
        "RED=''\n"
        "NC=''\n"
        "PUBLIC_BASE_URL='https://offer.example.test'\n"
        + function
        + "\n"
        + "CURL_CALLS=0\n"
        + "curl() {\n"
        + "    echo CURL_CALL\n"
        + "    CURL_CALLS=$((CURL_CALLS+1))\n"
        + f"    if [ {fail_times} -gt 0 ] && "
        + f"[ \"$CURL_CALLS\" -le {fail_times} ]; then\n"
        + f"        return {fail_status}\n"
        + "    fi\n"
        + "    return 0\n"
        + "}\n"
        + "sleep() { :; }\n"
        # The caller form: the check is invoked in a CONDITION, so `set -e`
        # cannot abort the script before the failure message is printed.
        + "if ! verify_offer_certificate; then\n"
        + "    echo CALLER_RC=nonzero\n"
        + "    exit 1\n"
        + "fi\n"
        + "echo CALLER_RC=zero\n"
    )
    result = _run_bash_raw(body)
    result["calls"] = result["stdout"].count("CURL_CALL")
    result["output"] = result["stdout"] + result["stderr"]
    return result


class TestTheDeployGateCertificateCheckRunsForReal:
    """The check as the deploy host runs it, not as this file imagines it.

    MEASURED DEFECT (2026-11-12): the check made ONE request with no
    retry -- the only check in update.sh without one, while its two
    siblings use a bounded three attempts -- and reported EVERY failure as
    "the TLS certificate did not verify". curl's exit codes are distinct
    (60 = the certificate does not verify, 7 = connection refused, 28 =
    timeout) and only 60 is a certificate failure, so a caddy that was
    still coming up after `docker compose restart caddy` turned a GOOD
    deploy into a red "certificate did not verify" and `exit 1`.

    Every test below runs the REAL function out of update.sh (AGENTS.md:
    "Verify against the real function, not a reimplementation").
    """

    def test_a_certificate_that_verifies_passes_silently(self):
        result = _run_certificate_check()
        assert result["rc"] == 0, result["output"]
        assert "CALLER_RC=zero" in result["stdout"], result["output"]
        assert result["calls"] == 1, (
            f"the check made {result['calls']} requests although the first one "
            "succeeded; a check that passes must not keep asking"
        )
        assert "[!]" not in result["output"], (
            "the check passed and still printed an error: " + result["output"]
        )

    def test_a_certificate_that_does_not_verify_is_reported_as_such(self):
        """curl exit 60 IS a certificate failure, and it says so."""
        result = _run_certificate_check(fail_times=3, fail_status=60)
        assert result["rc"] != 0, result["output"]
        assert "CALLER_RC=nonzero" in result["stdout"], result["output"]
        assert "did not verify" in result["output"], (
            "curl failed with 60 (the certificate does not verify) and the "
            "check did not say so: " + result["output"]
        )
        assert "did not answer" not in result["output"], (
            "a certificate failure was also reported as the site not "
            "answering: " + result["output"]
        )

    def test_a_site_that_does_not_answer_is_not_a_certificate_failure(self):
        """curl exit 7 is "connection refused", NOT a broken certificate.

        This is the false failure the defect produced: a caddy that was
        still coming up was reported as a certificate that did not verify,
        which sent the operator to fix a certificate that was fine.
        """
        result = _run_certificate_check(fail_times=3, fail_status=7)
        assert result["rc"] != 0, result["output"]
        assert "CALLER_RC=nonzero" in result["stdout"], result["output"]
        assert "did not answer" in result["output"], (
            "curl failed with 7 (connection refused) and the check did not "
            "say the site did not answer: " + result["output"]
        )
        assert "did not verify" not in result["output"], (
            "curl failed with 7 (connection refused) and the check reported "
            "it as a CERTIFICATE failure, which sends the operator to fix a "
            "certificate that is fine: " + result["output"]
        )

    def test_a_timeout_is_not_a_certificate_failure(self):
        """curl exit 28 is a timeout -- also not the certificate."""
        result = _run_certificate_check(fail_times=3, fail_status=28)
        assert result["rc"] != 0, result["output"]
        assert "did not answer" in result["output"], result["output"]
        assert "did not verify" not in result["output"], result["output"]

    def test_the_check_retries_a_bounded_number_of_times(self):
        """Bounded: exactly three attempts, like the two sibling checks.

        Not ">1": a check that asked a hundred times would hang the deploy
        instead of failing it.
        """
        result = _run_certificate_check(fail_times=3, fail_status=60)
        assert result["calls"] == 3, (
            f"the check made {result['calls']} requests; it must make exactly "
            "three -- one attempt would report a caddy that was still coming "
            "up as a failure, and more would hang the deploy"
        )

    def test_a_caddy_that_was_still_coming_up_is_rescued_by_the_retry(self):
        """The first request fails, the second answers: that is a SUCCESS."""
        result = _run_certificate_check(fail_times=1, fail_status=7)
        assert result["rc"] == 0, (
            "the check failed although the site answered on the second "
            "attempt: " + result["output"]
        )
        assert "CALLER_RC=zero" in result["stdout"], result["output"]
        assert result["calls"] == 2, (
            f"the check made {result['calls']} requests; it must stop as soon "
            "as one of them succeeds"
        )
        assert "[!]" not in result["output"], result["output"]


# --------------------------------------------------------------------------
# The smoke test's certificate check, RUN
# --------------------------------------------------------------------------

# The name of the smoke test's certificate check. It has to be DISTINCT from
# the deploy gate's (verify_offer_certificate): the two scripts are checked
# separately, and a test that found the wrong function would prove nothing.
SMOKE_CERT_FUNCTION = "check_offer_certificate"


def _run_smoke_certificate_check(fail_times: int = 0, fail_status: int = 0) -> dict:
    """The REAL check, lifted out of smoke-test.sh and run under bash.

    The stub `curl` COUNTS its calls -- so the retry bound is MEASURED, not
    assumed -- and returns a chosen exit status, so the 60-vs-other
    distinction is measured too. The stub `sleep` returns immediately, so
    the retry test does not spend ten real seconds.

    `fail_times` is how many of the FIRST calls fail; the calls after that
    succeed. `fail_status` is the curl exit code those calls return.

    The smoke test runs under `set -u` and NOT `set -e`, and that is what is
    reproduced here: `set -e` would abort the snippet on the check's own
    non-zero return and hide the failure message it had just printed.
    """
    function = _extract_function(
        SMOKE_TEST.read_text(encoding="utf-8"), SMOKE_CERT_FUNCTION
    )
    body = (
        "set -u\n"
        "FAIL=0\n"
        "DOMAIN='https://offer.example.test'\n"
        + function
        + "\n"
        + "CURL_CALLS=0\n"
        + "curl() {\n"
        + "    echo CURL_CALL\n"
        + "    CURL_CALLS=$((CURL_CALLS+1))\n"
        + f"    if [ {fail_times} -gt 0 ] && "
        + f"[ \"$CURL_CALLS\" -le {fail_times} ]; then\n"
        + f"        return {fail_status}\n"
        + "    fi\n"
        + "    return 0\n"
        + "}\n"
        + "sleep() { :; }\n"
        # The check is called the way the smoke test calls it -- as the
        # CONDITION of an `if`, and its RETURN VALUE is read afterwards.
        # An `exit` inside the function would kill the snippet before these
        # two lines ran, so their presence proves the function returns.
        + "if check_offer_certificate; then\n"
        + "    RC=0\n"
        + "else\n"
        + "    RC=1\n"
        + "fi\n"
        + 'echo "RC=$RC"\n'
        + 'echo "FAIL=$FAIL"\n'
    )
    result = _run_bash_raw(body)
    result["calls"] = result["stdout"].count("CURL_CALL")
    result["output"] = result["stdout"] + result["stderr"]
    return result


class TestTheSmokeTestCertificateCheckRunsForReal:
    """The check as the smoke test runs it, not as this file imagines it.

    MEASURED DEFECT (FINDING H, 2026-11-15): the smoke test's certificate
    check was `if ! curl ...; then`, which (a) cannot read curl's exit
    status at all -- inside `if ! cmd` the `$?` is the status of the
    NEGATION (0), not of the command -- so EVERY failure was reported as
    "the TLS certificate did not verify", and (b) made ONE request with no
    retry, while the check it was modelled on (update.sh's
    verify_offer_certificate) and both of that script's siblings use a
    bounded three attempts.

    The false failure it produced: the smoke test runs every five minutes
    from ops/loop.sh, so a caddy that was briefly unavailable (curl exit 7,
    connection refused) was reported as a broken certificate and sent the
    operator to fix a certificate that was perfectly fine.

    Every test below runs the REAL function out of smoke-test.sh (AGENTS.md:
    "Verify against the real function, not a reimplementation").
    """

    def test_a_certificate_that_verifies_passes_silently(self):
        result = _run_smoke_certificate_check()
        assert result["rc"] == 0, result["output"]
        assert "RC=0" in result["stdout"], result["output"]
        assert result["calls"] == 1, (
            f"the check made {result['calls']} requests although the first one "
            "succeeded; a check that passes must not keep asking"
        )
        assert "[FAIL]" not in result["output"], (
            "the check passed and still printed an error: " + result["output"]
        )

    def test_a_certificate_that_does_not_verify_is_reported_as_such(self):
        """curl exit 60 IS a certificate failure, and it says so."""
        result = _run_smoke_certificate_check(fail_times=3, fail_status=60)
        assert result["rc"] == 0, result["output"]
        assert "RC=1" in result["stdout"], result["output"]
        assert "did not verify" in result["output"], (
            "curl failed with 60 (the certificate does not verify) and the "
            "check did not say so: " + result["output"]
        )
        assert "did not answer" not in result["output"], (
            "a certificate failure was also reported as the site not "
            "answering: " + result["output"]
        )

    def test_a_site_that_does_not_answer_is_not_a_certificate_failure(self):
        """curl exit 7 is "connection refused", NOT a broken certificate.

        This is the false failure the defect produced: a caddy that was
        briefly unavailable was reported as a certificate that did not
        verify, which sent the operator to fix a certificate that was fine.
        """
        result = _run_smoke_certificate_check(fail_times=3, fail_status=7)
        assert result["rc"] == 0, result["output"]
        assert "RC=1" in result["stdout"], result["output"]
        assert "did not answer" in result["output"], (
            "curl failed with 7 (connection refused) and the check did not "
            "say the site did not answer: " + result["output"]
        )
        assert "did not verify" not in result["output"], (
            "curl failed with 7 (connection refused) and the check reported "
            "it as a CERTIFICATE failure, which sends the operator to fix a "
            "certificate that is fine: " + result["output"]
        )

    def test_a_timeout_is_not_a_certificate_failure(self):
        """curl exit 28 is a timeout -- also not the certificate."""
        result = _run_smoke_certificate_check(fail_times=3, fail_status=28)
        assert result["rc"] == 0, result["output"]
        assert "RC=1" in result["stdout"], result["output"]
        assert "did not answer" in result["output"], result["output"]
        assert "did not verify" not in result["output"], result["output"]

    def test_the_check_retries_a_bounded_number_of_times(self):
        """Bounded: exactly three attempts, like the sibling checks.

        Not ">1": a check that asked a hundred times would hang the smoke
        test instead of failing it.
        """
        result = _run_smoke_certificate_check(fail_times=3, fail_status=60)
        assert result["calls"] == 3, (
            f"the check made {result['calls']} requests; it must make exactly "
            "three -- one attempt would report a caddy that was still coming "
            "up as a failure, and more would hang the smoke test"
        )

    def test_a_caddy_that_was_still_coming_up_is_rescued_by_the_retry(self):
        """The first request fails, the second answers: that is a SUCCESS."""
        result = _run_smoke_certificate_check(fail_times=1, fail_status=7)
        assert result["rc"] == 0, result["output"]
        assert "RC=0" in result["stdout"], result["output"]
        assert result["calls"] == 2, (
            f"the check made {result['calls']} requests; it must stop as soon "
            "as one of them succeeds"
        )
        assert "[FAIL]" not in result["output"], result["output"]

    def test_the_check_sets_the_failure_counter_and_does_not_exit(self):
        """It sets FAIL=1 and RETURNS -- it must not exit the smoke test.

        The smoke test has more checks after this one, and the alert file is
        only written when the script runs to the end.
        """
        result = _run_smoke_certificate_check(fail_times=3, fail_status=60)
        assert "FAIL=1" in result["stdout"], (
            "the check failed but did not set the smoke test's own FAIL "
            "counter, so the smoke test would carry on as a success: "
            + result["output"]
        )
        assert "RC=1" in result["stdout"], (
            "the check did not RETURN a non-zero status, so the caller "
            "cannot act on it: " + result["output"]
        )


def test_the_smoke_test_certificate_check_is_a_function():
    """The check has to be a function, and it has to be the REAL one.

    A copy of the logic in this file would prove nothing about the script
    (AGENTS.md: "Verify against the real function, not a reimplementation"),
    so the name is looked up in the script itself.
    """
    text = SMOKE_TEST.read_text(encoding="utf-8")
    assert text.count(f"{SMOKE_CERT_FUNCTION}() {{") == 1, (
        f"smoke-test.sh must define exactly one {SMOKE_CERT_FUNCTION}() "
        f"function; it has {text.count(f'{SMOKE_CERT_FUNCTION}() {{')}"
    )
    assert text.count(f"if {SMOKE_CERT_FUNCTION}; then") == 1, (
        f"smoke-test.sh must call {SMOKE_CERT_FUNCTION}() exactly once, in a "
        "condition, or the check cannot fail the smoke test"
    )


def test_the_smoke_test_does_not_read_the_status_inside_a_negation():
    """`if ! curl` cannot report WHICH failure it was.

    Inside `if ! cmd` the `$?` is the status of the NEGATION (0) and not of
    the command, so the exit code of curl -- the thing that tells a
    certificate failure (60) apart from a site that is not answering (7,
    28) -- cannot be read from there at all. The old check was exactly this
    shape, and it reported every failure as a certificate failure.
    """
    text = SMOKE_TEST.read_text(encoding="utf-8")
    assert 'if ! curl -s -o /dev/null --max-time 10 "$DOMAIN/source"' not in text, (
        "the smoke test still reads its certificate check as `if ! curl ...`: "
        "inside a negation $? is the status of the negation (0), not of "
        "curl, so a site that is not answering is reported as a certificate "
        "failure"
    )


def _run_the_whole_smoke_test(cert_status: int = 60) -> dict:
    """The REAL smoke test, end to end, with the network stubbed out.

    The script is fed to bash verbatim over stdin (the same way the deploy
    gate's script is exercised), with the network commands stubbed: psql
    succeeds, and curl answers with a status code for the liveness checks
    and with the CHOSEN exit status for the certificate request against
    /source.

    This is what proves the smoke test still finishes: it has to run its
    remaining checks and write its alert file after the certificate check
    has failed.
    """
    script = SMOKE_TEST.read_text(encoding="utf-8")
    prelude = (
        "STATE_DIR=$(mktemp -d)\n"
        "export STATE_DIR\n"
        "export PUBLIC_BASE_URL='https://offer.example.test'\n"
        "export POSTGRES_PASSWORD='stub'\n"
        "export DEPLOY_ENV_FILE=\"$STATE_DIR/there-is-no.env\"\n"
        "psql() { return 0; }\n"
        "curl() {\n"
        "    url=''\n"
        "    for a in \"$@\"; do\n"
        "        case \"$a\" in http*) url=\"$a\" ;; esac\n"
        "    done\n"
        "    for a in \"$@\"; do\n"
        "        if [ \"$a\" = '-w' ]; then\n"
        "            case \"$url\" in\n"
        "                */ws) echo -n '404' ;;\n"
        "                *) echo -n '200' ;;\n"
        "            esac\n"
        "            return 0\n"
        "        fi\n"
        "    done\n"
        "    case \"$url\" in\n"
        f"        *'/source') return {cert_status} ;;\n"
        "    esac\n"
        "    return 0\n"
        "}\n"
        "sleep() { :; }\n"
    )
    after = (
        'echo "SCRIPT_RC=$SCRIPT_RC"\n'
        'if [ -f "$STATE_DIR/alert" ]; then\n'
        '    echo "ALERT_EXISTS=1"\n'
        '    echo "---ALERT---"\n'
        '    cat "$STATE_DIR/alert"\n'
        'else\n'
        '    echo "ALERT_EXISTS=0"\n'
        'fi\n'
        'echo "---LOG---"\n'
        'cat "$STATE_DIR/smoke.log" 2>/dev/null\n'
    )
    body = (
        prelude
        + "(\n"
        + script
        + "\n)\n"
        + "SCRIPT_RC=$?\n"
        + after
    )
    result = _run_bash_raw(body)
    result["output"] = result["stdout"] + result["stderr"]
    return result


@pytest.mark.parametrize(
    "cert_status,expected,forbidden",
    [
        (60, "the TLS certificate did not verify", "did not answer"),
        (7, "did not answer", "did not verify"),
    ],
    ids=["certificate-does-not-verify", "site-does-not-answer"],
)
def test_the_smoke_test_keeps_running_and_writes_its_alert_after_a_certificate_failure(
    cert_status, expected, forbidden
):
    """A failed certificate check must not stop the smoke test dead.

    The smoke test has more checks after the certificate one, and the alert
    file -- the thing the operator actually reads -- is only written when
    the script runs to the end. A check that exited instead of returning
    would leave the operator with no alert at all.

    Both failure kinds are run: with the old `if ! curl` check, a site that
    was not answering (7) was reported as a broken certificate, so the
    message is asserted as well as the continuation.
    """
    result = _run_the_whole_smoke_test(cert_status=cert_status)
    assert "SCRIPT_RC=1" in result["stdout"], (
        "the smoke test did not report a failure even though the certificate "
        "check failed: " + result["output"]
    )
    assert expected in result["output"], (
        f"the smoke test did not report the failure as '{expected}': "
        + result["output"]
    )
    assert forbidden not in result["output"], (
        f"the smoke test reported the failure as '{forbidden}' as well: "
        + result["output"]
    )
    assert "ALERT_EXISTS=1" in result["stdout"], (
        "the smoke test did not write its alert file after the certificate "
        "failure: " + result["output"]
    )
    assert "smoke test FAILED" in result["output"], result["output"]
    assert "SKIP the source archive check" in result["output"], (
        "the smoke test stopped at the certificate check instead of carrying "
        "on to its remaining checks: " + result["output"]
    )
    assert "postgres reachable" in result["output"], (
        "the checks before the certificate one no longer run: " + result["output"]
    )
