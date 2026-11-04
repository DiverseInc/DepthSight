"""The Google sign-in endpoint: no credential in a log, and ONE session per redirect.

Two separate guarantees are pinned here.

B3 -- THE CREDENTIAL IS NEVER LOGGED
------------------------------------
``api/routes/auth.py`` used to log the failure of a Google token
verification as ``f"Failed to verify Google token: {e}"``. The exception
message is NOT safe: ``google.auth.jwt._unverified_decode`` raises

    MalformedError("Wrong number of segments in token: b'<the token>'")

so ``e`` carries the raw bearer credential, and the application log got
the credential in full. The audit log then got the first TEN CHARACTERS of
it. Both are now a truncated sha256 digest of the token, which identifies
the attempt without being usable to authenticate.

B2 -- ONE REDIRECT PRODUCES EXACTLY ONE SESSION
------------------------------------------------
A double submit -- a remount, a reload, a second tab that received the
same redirect -- must not mint a second session. The server consumes the
one-time value the client generated exactly once, and it consumes the
credential (by its digest) exactly once, so a replayed credential is
refused too.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient

GOOGLE_CLIENT_ID = "test-client.apps.googleusercontent.com"

# Where the audit logger writes. It is created when api.audit_logger is
# imported, so it normally exists by the time a test runs.
AUDIT_LOG_PATH = Path("logs/security_audit.log")


@pytest.fixture
def google_configured(monkeypatch):
    """The endpoint refuses to do anything without a configured client id."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", GOOGLE_CLIENT_ID)


@pytest.fixture
def no_certificate_fetch(monkeypatch):
    """Stop google-auth from fetching Google's signing certificates.

    The certificate fetch is the ONLY network call in the verification path
    and it happens BEFORE the token is parsed, so without this a malformed
    token would reach out to the network before failing.
    """
    monkeypatch.setattr(
        "google.oauth2.id_token._fetch_certs",
        lambda request, certs_url: {},
    )


@pytest.fixture
def valid_google_token(monkeypatch):
    """Make the verification succeed, without touching Google."""

    def _verify(token, request, client_id):
        assert client_id == GOOGLE_CLIENT_ID
        return {
            "iss": "https://accounts.google.com",
            "email": f"signin-{uuid.uuid4().hex[:8]}@example.com",
        }

    monkeypatch.setattr(
        "api.routes.auth.id_token.verify_oauth2_token",
        _verify,
    )


# ---------------------------------------------------------------------------
# B3 -- the credential is never written to a log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_verification_never_writes_the_credential_to_a_log(
    test_client: AsyncClient,
    caplog,
    google_configured,
    no_certificate_fetch,
):
    """The credential must not reach the application log OR the audit log."""
    # Unique per run, so a credential left in the audit log by an EARLIER
    # (unfixed) run cannot make this fail for the wrong reason.
    credential = f"ds-google-id-token-{uuid.uuid4().hex}"

    audit_before = (
        AUDIT_LOG_PATH.read_text(encoding="utf-8", errors="replace")
        if AUDIT_LOG_PATH.exists()
        else ""
    )

    with caplog.at_level(logging.DEBUG):
        response = await test_client.post(
            "/api/v1/auth/google",
            json={"token": credential, "signin_nonce": "nonce-b3"},
        )

    assert response.status_code == 401, response.text

    logged = "\n".join(record.getMessage() for record in caplog.records)

    # The credential itself.
    assert credential not in logged, (
        "the credential was written to a log. The verification library puts "
        "the credential in the exception message, so the message must not be "
        "logged either."
    )
    # And any prefix of it.
    assert credential[:10] not in logged, (
        "a prefix of the credential was written to a log."
    )

    # The audit log FILE must not carry it either: the file handler writes
    # the same record the caplog captured.
    if AUDIT_LOG_PATH.exists():
        audit_after = AUDIT_LOG_PATH.read_text(encoding="utf-8", errors="replace")
        written = audit_after[len(audit_before) :]
        assert credential not in written, (
            "the credential was written to the audit log file."
        )
        assert credential[:10] not in written, (
            "a prefix of the credential was written to the audit log file."
        )


@pytest.mark.asyncio
async def test_the_audit_entry_still_identifies_the_attempt(
    test_client: AsyncClient,
    caplog,
    google_configured,
    no_certificate_fetch,
):
    """The fix must not be "delete the log line".

    The attempt is still identifiable -- by a truncated sha256 digest of the
    credential, which cannot be used to authenticate.
    """
    credential = f"ds-google-id-token-{uuid.uuid4().hex}"
    digest = hashlib.sha256(credential.encode("utf-8")).hexdigest()

    with caplog.at_level(logging.DEBUG):
        await test_client.post(
            "/api/v1/auth/google",
            json={"token": credential, "signin_nonce": "nonce-b3-digest"},
        )

    logged = "\n".join(record.getMessage() for record in caplog.records)

    assert digest[:16] in logged, (
        "the log must still identify the attempt by a truncated sha256 digest "
        "of the credential."
    )
    # The audit entry identifies the attempt the same way.
    audit_entries = [
        record.getMessage()
        for record in caplog.records
        if record.name == "security_audit"
    ]
    assert audit_entries, "the failed sign-in must still be audited"
    audit = json.loads(audit_entries[-1])
    assert audit["username"] == f"google_{digest[:12]}"
    assert credential not in audit["username"]
    assert credential[:10] not in audit["username"]


# ---------------------------------------------------------------------------
# B2 -- one redirect produces exactly one session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_same_redirect_cannot_mint_a_second_session(
    test_client: AsyncClient,
    google_configured,
    valid_google_token,
):
    """The second attempt with the same one-time value is refused."""
    body = {
        "token": f"ds-google-id-token-{uuid.uuid4().hex}",
        "signin_nonce": f"nonce-{uuid.uuid4().hex}",
    }

    first = await test_client.post("/api/v1/auth/google", json=body)
    assert first.status_code == 200, first.text

    second = await test_client.post("/api/v1/auth/google", json=body)
    assert second.status_code == 409, (
        "the second attempt with the same one-time value must be refused, "
        f"not mint a second session. Got {second.status_code}: {second.text}"
    )


@pytest.mark.asyncio
async def test_a_replayed_credential_is_refused_even_with_a_new_one_time_value(
    test_client: AsyncClient,
    google_configured,
    valid_google_token,
):
    """The credential is single-use in its own right.

    A replayed credential is still a valid credential until it expires, so
    the credential itself is consumed as well as the one-time value.
    """
    credential = f"ds-google-id-token-{uuid.uuid4().hex}"

    first = await test_client.post(
        "/api/v1/auth/google",
        json={"token": credential, "signin_nonce": f"nonce-{uuid.uuid4().hex}"},
    )
    assert first.status_code == 200, first.text

    second = await test_client.post(
        "/api/v1/auth/google",
        json={"token": credential, "signin_nonce": f"nonce-{uuid.uuid4().hex}"},
    )
    assert second.status_code == 409, (
        "a replayed credential must not mint a second session."
    )


@pytest.mark.asyncio
async def test_a_client_that_sends_no_one_time_value_is_still_single_use(
    test_client: AsyncClient,
    google_configured,
    valid_google_token,
):
    """The PWA popup flow sends no one-time value, and must still be safe."""
    body = {"token": f"ds-google-id-token-{uuid.uuid4().hex}"}

    first = await test_client.post("/api/v1/auth/google", json=body)
    assert first.status_code == 200, first.text

    second = await test_client.post("/api/v1/auth/google", json=body)
    assert second.status_code == 409, (
        "a credential must not be exchangeable twice, whatever the client sent."
    )


@pytest.mark.asyncio
async def test_the_one_time_value_is_spent_even_with_a_different_credential(
    test_client: AsyncClient,
    google_configured,
    valid_google_token,
):
    """The one-time value is consumed on its own, not only with its credential.

    The client generates a FRESH one-time value for every attempt, so
    refusing a spent one-time value never blocks a legitimate retry -- it
    stops a replayed response from being exchanged a second time.
    """
    nonce = f"nonce-{uuid.uuid4().hex}"

    first = await test_client.post(
        "/api/v1/auth/google",
        json={"token": f"ds-google-id-token-{uuid.uuid4().hex}", "signin_nonce": nonce},
    )
    assert first.status_code == 200, first.text

    second = await test_client.post(
        "/api/v1/auth/google",
        json={
            "token": f"ds-google-id-token-{uuid.uuid4().hex}",
            "signin_nonce": nonce,
        },
    )
    assert second.status_code == 409, (
        "a one-time value that has already been consumed must not be "
        "accepted again."
    )


@pytest.mark.asyncio
async def test_two_different_sign_ins_are_both_accepted(
    test_client: AsyncClient,
    google_configured,
    valid_google_token,
):
    """Single-use must not break the ordinary case: two real sign-ins work.

    Two users (or the same user in two browsers) sign in at the same time.
    Each has its own one-time value and its own credential, so each must
    succeed.
    """
    first = await test_client.post(
        "/api/v1/auth/google",
        json={
            "token": f"ds-google-id-token-{uuid.uuid4().hex}",
            "signin_nonce": f"nonce-{uuid.uuid4().hex}",
        },
    )
    assert first.status_code == 200, first.text

    second = await test_client.post(
        "/api/v1/auth/google",
        json={
            "token": f"ds-google-id-token-{uuid.uuid4().hex}",
            "signin_nonce": f"nonce-{uuid.uuid4().hex}",
        },
    )
    assert second.status_code == 200, second.text
