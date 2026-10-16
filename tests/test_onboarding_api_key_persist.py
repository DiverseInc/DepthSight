"""
Regression tests for the onboarding wizard's OKX key step.

THE DEFECT: the wizard called `POST /onboarding/validate-okx-key`, which by design
does not store the credential, then told the user "OKX key validated — live
trading on OKX will be enabled". Nothing was stored. The very next step called
`convert-to-live`, which looks for an active OKX key, found none, and returned
400 "No active OKX key. Add one in Settings → API Keys first." A user who
followed every step of the wizard still could not finish — the UI promised a
save the backend never performed.

These tests pin the three properties that make the flow actually work:

1. Valid credentials ARE persisted, and under the right environment suffix.
2. Invalid credentials are NEVER persisted (a dead key must not reach the DB).
3. Persistence goes through the canonical `add_api_key` handler rather than a
   second implementation that could drift from it.

They use the real in-memory test DB and the real `add_api_key` handler — only
the OKX network call and the achievements helper are stubbed.
"""

import pytest
from unittest.mock import patch, AsyncMock
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

import ccxt.async_support as ccxt_async

from api import models
from api.routes.onboarding import (
    ValidateOkxKeyRequest,
    connect_okx_key,
    validate_okx_key,
)

# No `pytestmark = pytest.mark.asyncio`: pytest.ini sets `asyncio_mode = auto`,
# so bare `async def` tests are collected already (AGENTS.md line 14).


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_BALANCE = {"info": {"data": [{"acctGrp": "VIP5"}]}}


class _FakeOkxExchange:
    """Stands in for a ccxt OKX exchange. Records the sandbox flag so tests can
    assert a testnet credential was checked against testnet."""

    def __init__(self, balance=None, error=None):
        self._balance = balance if balance is not None else VALID_BALANCE
        self._error = error
        self.sandbox_mode = None
        self.closed = False

    def set_sandbox_mode(self, enabled):
        self.sandbox_mode = enabled

    async def fetch_balance(self):
        if self._error is not None:
            raise self._error
        return self._balance

    async def close(self):
        self.closed = True


def _make_request() -> Request:
    """A minimal real Starlette Request — `add_api_key` reads the client IP off it."""
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/onboarding/connect-okx-key",
            "headers": [(b"host", b"testserver")],
            "client": ("1.2.3.4", 51000),
            "query_string": b"",
        }
    )


def _patch_exchange(exchange: _FakeOkxExchange):
    """Patch the ccxt constructor used inside api.routes.onboarding."""
    return patch(
        "api.routes.onboarding.ccxt_async.okx", return_value=exchange
    )


async def _stored_keys(db: AsyncSession, user) -> list:
    """Every active OKX key on the account, across both environments.

    `list_active_api_keys_for_user` filters by exchange, and an OKX key is
    stored as either "okx" or "okx_testnet" depending on the toggle — so a
    single-environment query would miss a key stored on the other one.
    """
    from api import crud

    keys = []
    for exchange in ("okx", "okx_testnet"):
        keys.extend(
            await crud.list_active_api_keys_for_user(
                db, user_id=user.id, exchange=exchange
            )
        )
    return keys


@pytest.fixture(autouse=True)
def mock_achievement_grant():
    """`add_api_key` grants an achievement on the first key; keep it off the DB."""
    mock = AsyncMock()
    with patch("api.routes.api_keys.grant_achievement", mock):
        yield mock


@pytest.fixture(autouse=True)
def mock_redis_factory():
    """The onboarding endpoint calls `await get_redis_client()` to hand the key
    to the canonical handler. AsyncMock wholesale — a hand-picked mock breaks the
    moment a second awaited redis command appears (the exact failure that took
    down PR #5 and then PR #14)."""
    with patch(
        "api.routes.onboarding.get_redis_client",
        new=AsyncMock(return_value=AsyncMock()),
    ):
        yield


def _request(is_testnet: bool = True) -> ValidateOkxKeyRequest:
    return ValidateOkxKeyRequest(
        api_key="test-api-key",
        api_secret="test-api-secret",
        passphrase="test-passphrase",
        is_testnet=is_testnet,
    )


# ---------------------------------------------------------------------------
# 1. The core fix: a valid credential is actually stored
# ---------------------------------------------------------------------------

async def test_connect_persists_a_testnet_key(
    db_session: AsyncSession, test_user: models.User
):
    """The regression itself: a valid demo key must survive the wizard."""
    exchange = _FakeOkxExchange()
    with _patch_exchange(exchange):
        result = await connect_okx_key(
            request=_make_request(),
            req=_request(is_testnet=True),
            current_user=test_user,
            db=db_session,
        )

    data = result["data"]
    assert data.valid is True
    assert data.api_key_id is not None, "wizard was told the key was fine but got no id"

    stored = await _stored_keys(db_session, test_user)
    assert len(stored) == 1, f"expected exactly one stored key, got {len(stored)}"
    assert stored[0].id == data.api_key_id
    # Environment suffix is load-bearing: the bot keys sandbox mode off it.
    assert stored[0].exchange == "okx_testnet"
    assert data.exchange == "okx_testnet"


async def test_connect_persists_a_mainnet_key(
    db_session: AsyncSession, test_user: models.User
):
    exchange = _FakeOkxExchange()
    with _patch_exchange(exchange):
        result = await connect_okx_key(
            request=_make_request(),
            req=_request(is_testnet=False),
            current_user=test_user,
            db=db_session,
        )

    assert result["data"].valid is True
    stored = await _stored_keys(db_session, test_user)
    assert len(stored) == 1
    assert stored[0].exchange == "okx"
    assert exchange.sandbox_mode is None, "a mainnet key must not be sandboxed"


async def test_connect_sends_testnet_credentials_to_testnet(
    db_session: AsyncSession, test_user: models.User
):
    """A testnet key must be sanity-read against testnet, or it authenticates
    against mainnet, fails, and the wizard pushes the user toward a mainnet key."""
    exchange = _FakeOkxExchange()
    with _patch_exchange(exchange):
        await connect_okx_key(
            request=_make_request(),
            req=_request(is_testnet=True),
            current_user=test_user,
            db=db_session,
        )
    assert exchange.sandbox_mode is True


async def test_connect_returns_the_okx_account_type(
    db_session: AsyncSession, test_user: models.User
):
    exchange = _FakeOkxExchange()
    with _patch_exchange(exchange):
        result = await connect_okx_key(
            request=_make_request(),
            req=_request(),
            current_user=test_user,
            db=db_session,
        )
    assert result["data"].account_type == "VIP5"


# ---------------------------------------------------------------------------
# 2. A dead credential must never be stored
# ---------------------------------------------------------------------------

async def test_connect_does_not_persist_when_authentication_fails(
    db_session: AsyncSession, test_user: models.User
):
    exchange = _FakeOkxExchange(error=ccxt_async.AuthenticationError("bad secret"))
    with _patch_exchange(exchange):
        result = await connect_okx_key(
            request=_make_request(),
            req=_request(),
            current_user=test_user,
            db=db_session,
        )

    data = result["data"]
    assert data.valid is False
    assert data.api_key_id is None
    assert await _stored_keys(db_session, test_user) == []
    assert exchange.closed is True, "failed validation must still close the exchange"


async def test_connect_does_not_persist_when_okx_is_unreachable(
    db_session: AsyncSession, test_user: models.User
):
    exchange = _FakeOkxExchange(error=ConnectionError("no route to host"))
    with _patch_exchange(exchange):
        result = await connect_okx_key(
            request=_make_request(),
            req=_request(),
            current_user=test_user,
            db=db_session,
        )

    assert result["data"].valid is False
    assert result["data"].api_key_id is None
    assert await _stored_keys(db_session, test_user) == []


async def test_validation_failure_survives_a_raising_constructor(
    db_session: AsyncSession, test_user: models.User
):
    """The old inline version referenced `exchange` in its `except` blocks
    without binding it first: if the ccxt constructor itself raised, cleanup
    raised NameError and masked the real error. `exchange` is now None-safe."""
    with patch(
        "api.routes.onboarding.ccxt_async.okx",
        side_effect=RuntimeError("constructor exploded"),
    ):
        result = await connect_okx_key(
            request=_make_request(),
            req=_request(),
            current_user=test_user,
            db=db_session,
        )

    assert result["data"].valid is False
    assert "constructor exploded" in result["data"].error
    assert await _stored_keys(db_session, test_user) == []


# ---------------------------------------------------------------------------
# 3. Validation-only stays validation-only
# ---------------------------------------------------------------------------

async def test_validate_endpoint_still_does_not_persist(
    db_session: AsyncSession, test_user: models.User
):
    """`validate-okx-key` is a legitimately different action ("check before I
    commit"). Guard against a future 'helpfully' making it save."""
    exchange = _FakeOkxExchange()
    with _patch_exchange(exchange):
        result = await validate_okx_key(req=_request(), current_user=test_user)

    assert result["data"].valid is True
    assert await _stored_keys(db_session, test_user) == []


# ---------------------------------------------------------------------------
# 4. Delegation, not reimplementation
# ---------------------------------------------------------------------------

async def test_connect_delegates_to_the_canonical_handler(
    db_session: AsyncSession, test_user: models.User
):
    """There must be exactly ONE place that persists an API key. If onboarding
    grew its own insert it would silently miss duplicate detection, the
    first-key achievement, referral activation, active-key selection, the
    ACTIVATE_API_KEY bot command and the audit log."""
    exchange = _FakeOkxExchange()
    sentinel = AsyncMock(
        return_value={"data": models.ApiKey(id=4242, exchange="okx_testnet")}
    )
    with _patch_exchange(exchange), patch(
        "api.routes.api_keys.add_api_key", new=sentinel
    ):
        result = await connect_okx_key(
            request=_make_request(),
            req=_request(is_testnet=True),
            current_user=test_user,
            db=db_session,
        )

    sentinel.assert_called_once()
    kwargs = sentinel.call_args.kwargs
    assert kwargs["api_key_data"].exchange == "okx_testnet"
    assert kwargs["api_key_data"].api_key == "test-api-key"
    assert kwargs["api_key_data"].api_password == "test-passphrase"
    assert result["data"].api_key_id == 4242


async def test_connect_name_passes_the_schema_validator(
    db_session: AsyncSession, test_user: models.User
):
    """`ApiKeyBase.name` is validated against ^[a-zA-Z0-9_\\s.-]*$ — parentheses
    are rejected. The onboarding-generated names must survive the validator,
    otherwise every connect fails at the last step."""
    from api import schemas

    for is_testnet in (True, False):
        name = (
            "OKX Demo onboarding" if is_testnet else "OKX Live onboarding"
        )
        created = schemas.ApiKeyCreate(
            name=name,
            exchange="okx_testnet" if is_testnet else "okx",
            api_key="k",
            api_secret="s",
            api_password="p",
        )
        assert created.name == name
