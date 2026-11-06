"""The $499 Managed service renews itself (owner decision, 2026-11-06).

The payment provider has NO provider-side recurring: Bitcart's Merchants
API has no recurring/subscription field anywhere in its published
specification, and its only checkout endpoint carries no recurring toggle.
So the invoice for the NEXT 30 days has to be issued by us.

The chain the owner approved:

    invoice N is paid -> the plan extends by its period -> invoice N+1 is
    issued -> invoice N+1 is paid -> ...

so the customer always has the next invoice in front of them, nobody has to
invoice by hand, and the service continues for exactly as long as the
customer keeps paying.

These tests drive the REAL webhook handler against a REAL payment row with
the payment provider stubbed out, and they prove the properties that
matter on a money path:

  * the renewal invoice is issued EXACTLY ONCE for a paid Managed period,
  * it carries the CURRENT effective price, the right order_id, and it
    resolves back to the right customer,
  * a REPLAYED confirmation issues no second invoice,
  * a tier that does not renew is left alone,
  * a tier that has been SWITCHED OFF is refused, and the refusal is LOGGED,
  * a PROVIDER FAILURE still confirms the payment and is LOGGED at ERROR.
"""

import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import requests
import yaml

from api import crud, models
from api.plans import PlansConfig, plans_config

REPO_ROOT = Path(__file__).resolve().parent.parent
PLANS_CONFIG = REPO_ROOT / "api" / "plans_config.yml"

WEBHOOK_SECRET = "test-webhook-secret"
BITCART_URL = "https://payments.example.test/bitcart-api"
BITCART_STORE_ID = "test-store-id"


# ---------------------------------------------------------------------------
# The stubs: a fake provider that records every invoice, and a fake request
# that carries a valid Bitcart signature.
# ---------------------------------------------------------------------------
class _FakeResponse:
    """What the payment provider sends back for an invoice it created."""

    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"provider returned {self.status_code}")

    def json(self):
        return self._payload


class _FakeRequest:
    """The FastAPI Request the webhook reads the body and the signature from."""

    def __init__(self, body: bytes, signature: str):
        self._body = body
        self.headers = {"X-Bitcart-Signature": signature}

    async def body(self) -> bytes:
        return self._body


@pytest.fixture
def provider(monkeypatch):
    """A stub payment provider that records every invoice it is asked for."""
    from api.routes import payments as payments_module

    calls: list[dict] = []

    def _post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "payload": json})
        return _FakeResponse(
            {
                "id": f"inv-{len(calls)}",
                "status": "pending",
                "payments": [],
                "expiration_seconds": 900,
            }
        )

    monkeypatch.setattr(payments_module.requests, "post", _post)
    return calls


@pytest.fixture
def provider_env(monkeypatch):
    """The provider configuration the invoice path reads from the environment."""
    monkeypatch.setenv("BITCART_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("BITCART_API_URL", BITCART_URL)
    monkeypatch.setenv("BITCART_API_KEY", "test-api-key")
    monkeypatch.setenv("BITCART_STORE_ID", BITCART_STORE_ID)
    monkeypatch.setenv("FRONTEND_BASE_URL", "https://app.example.test")
    monkeypatch.setenv("API_BASE_URL", "https://api.example.test/api/v1")


@pytest.fixture
async def managed_payment(db_session, current_user):
    """A Managed invoice sitting in the database, waiting for its payment."""
    payment = await crud.create_payment(
        db_session,
        user_id=current_user.id,
        plan_name="managed",
        amount_usd=plans_config.get_effective_plan_price("managed"),
    )
    await db_session.commit()
    await db_session.refresh(payment)
    return payment


def _signed_request(payload: dict) -> _FakeRequest:
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(
        WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return _FakeRequest(body, signature)


async def _deliver(db_session, payment_id: str, event: str = "invoice_completed"):
    """Deliver a signed Bitcart event to the REAL webhook handler."""
    from api.routes import webhooks as webhooks_module

    payload = {"event": event, "data": {"order_id": str(payment_id)}}
    return await webhooks_module.bitcart_webhook(
        request=_signed_request(payload),
        db=db_session,
        redis_client=AsyncMock(),
    )


def _config_with(tmp_path, name: str, mutate) -> PlansConfig:
    """The real config, with `mutate` applied to the parsed source."""
    source = yaml.safe_load(PLANS_CONFIG.read_text(encoding="utf-8"))
    mutate(source)
    path = tmp_path / name
    path.write_text(
        yaml.safe_dump(source, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return PlansConfig(path)


def _install_config(monkeypatch, config: PlansConfig) -> None:
    """Point the plans module AND the payments route at `config`.

    `api/routes/payments.py` imports `plans_config` by name, so patching
    `api.plans.plans_config` alone would NOT change the route's view of
    the price or the guards. Both have to be patched.
    """
    from api import plans as plans_module
    from api.routes import payments as payments_module

    monkeypatch.setattr(plans_module, "plans_config", config)
    monkeypatch.setattr(payments_module, "plans_config", config)


async def _open_invoices(db_session, user_id: int) -> list:
    from sqlalchemy import select

    result = await db_session.execute(
        select(models.Payment).filter(
            models.Payment.user_id == user_id,
            models.Payment.status == "PENDING",
        )
    )
    return list(result.scalars().all())


def _as_utc(value: datetime) -> datetime:
    """SQLite hands back a naive datetime; the server works in UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


# ---------------------------------------------------------------------------
# 1. The renewal itself.
# ---------------------------------------------------------------------------
class TestThePaidManagedPeriodIssuesItsRenewal:
    async def test_a_paid_managed_payment_issues_exactly_one_renewal_invoice(
        self, db_session, current_user, managed_payment, provider, provider_env
    ):
        result = await _deliver(db_session, managed_payment.id)

        assert result == {"status": "ok"}
        assert len(provider) == 1, "the renewal invoice was not issued exactly once"

    async def test_the_renewal_invoice_carries_the_current_effective_price(
        self, db_session, current_user, managed_payment, provider, provider_env
    ):
        await _deliver(db_session, managed_payment.id)

        payload = provider[0]["payload"]
        assert payload["price"] == pytest.approx(
            plans_config.get_effective_plan_price("managed")
        )
        assert payload["price"] == 499

    async def test_the_renewal_invoice_resolves_back_to_the_right_customer(
        self, db_session, current_user, managed_payment, provider, provider_env
    ):
        await _deliver(db_session, managed_payment.id)

        payload = provider[0]["payload"]
        assert payload["buyer_email"] == current_user.email
        assert payload["store_id"] == BITCART_STORE_ID
        assert payload["notification_url"].endswith("/webhooks/bitcart")

        # `order_id` is what the provider's webhook gives us back, so it has
        # to resolve to a payment row that belongs to the customer who paid.
        renewal = await crud.get_payment_by_id(db_session, payload["order_id"])
        assert renewal is not None, "the renewal invoice has no payment row"
        assert renewal.id != managed_payment.id
        assert renewal.user_id == current_user.id
        assert renewal.plan_name == "managed"
        assert renewal.amount_usd == pytest.approx(499)
        assert renewal.status == "PENDING"
        assert renewal.bitcart_id == "inv-1"

    async def test_the_paid_period_is_extended_by_the_tier_period(
        self, db_session, current_user, managed_payment, provider, provider_env
    ):
        await _deliver(db_session, managed_payment.id)

        user = await db_session.get(models.User, current_user.id)
        assert user.plan == "managed"
        assert user.plan_expires_at is not None

        period_days = plans_config.get_plan_billing("managed", "monthly")["period_days"]
        assert period_days == 30
        remaining = _as_utc(user.plan_expires_at) - datetime.now(timezone.utc)
        assert timedelta(days=29) < remaining <= timedelta(days=30)


# ---------------------------------------------------------------------------
# 2. Idempotency: a replayed event must not issue a second invoice.
# ---------------------------------------------------------------------------
class TestAReplayedConfirmationIssuesNoSecondInvoice:
    async def test_the_same_event_delivered_three_times_issues_one_invoice(
        self, db_session, current_user, managed_payment, provider, provider_env
    ):
        await _deliver(db_session, managed_payment.id)
        await _deliver(db_session, managed_payment.id)
        await _deliver(db_session, managed_payment.id, event="invoice_confirmed")

        assert len(provider) == 1, (
            "a replayed confirmation issued a second renewal invoice"
        )

    async def test_a_second_confirmation_leaves_exactly_one_open_invoice(
        self, db_session, current_user, managed_payment, provider, provider_env
    ):
        await _deliver(db_session, managed_payment.id)
        await _deliver(db_session, managed_payment.id, event="invoice_confirmed")

        open_invoices = await _open_invoices(db_session, current_user.id)
        assert len(open_invoices) == 1, (
            f"expected exactly one open invoice, found {len(open_invoices)}"
        )
        assert open_invoices[0].id != managed_payment.id


# ---------------------------------------------------------------------------
# 3. Only the Managed tier renews.
# ---------------------------------------------------------------------------
class TestOnlyTheManagedTierRenews:
    async def test_a_paid_pro_payment_issues_no_renewal_invoice(
        self, db_session, current_user, provider, provider_env
    ):
        payment = await crud.create_payment(
            db_session,
            user_id=current_user.id,
            plan_name="pro",
            amount_usd=plans_config.get_effective_plan_price("pro"),
        )
        await db_session.commit()
        await db_session.refresh(payment)

        result = await _deliver(db_session, payment.id)

        assert result == {"status": "ok"}
        assert provider == [], "a tier that does not renew was issued an invoice"

    async def test_a_paid_standard_payment_issues_no_renewal_invoice(
        self, db_session, current_user, provider, provider_env
    ):
        payment = await crud.create_payment(
            db_session,
            user_id=current_user.id,
            plan_name="standard",
            amount_usd=plans_config.get_effective_plan_price("standard"),
        )
        await db_session.commit()
        await db_session.refresh(payment)

        await _deliver(db_session, payment.id)

        assert provider == [], "a tier that does not renew was issued an invoice"


# ---------------------------------------------------------------------------
# 4. A tier that has been switched off is refused, and the refusal is LOGGED.
# ---------------------------------------------------------------------------
class TestASwitchedOffTierIsRefusedAndLogged:
    async def test_no_renewal_invoice_is_issued(
        self,
        db_session,
        current_user,
        managed_payment,
        provider,
        provider_env,
        monkeypatch,
        tmp_path,
        caplog,
    ):
        config = _config_with(
            tmp_path,
            "plans_config_switched_off.yml",
            lambda source: source["plans"]["managed"].update({"active": False}),
        )
        _install_config(monkeypatch, config)

        with caplog.at_level(logging.ERROR):
            result = await _deliver(db_session, managed_payment.id)

        assert result == {"status": "ok"}
        assert provider == [], (
            "a renewal invoice was issued for a tier that has been switched off"
        )

    async def test_the_refusal_is_logged_with_the_customer_id(
        self,
        db_session,
        current_user,
        managed_payment,
        provider,
        provider_env,
        monkeypatch,
        tmp_path,
        caplog,
    ):
        config = _config_with(
            tmp_path,
            "plans_config_switched_off.yml",
            lambda source: source["plans"]["managed"].update({"active": False}),
        )
        _install_config(monkeypatch, config)

        with caplog.at_level(logging.ERROR):
            await _deliver(db_session, managed_payment.id)

        errors = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.ERROR
        ]
        assert errors, "the refusal to renew was not logged at ERROR level"
        assert any(
            "renewal" in message.lower() and str(current_user.id) in message
            for message in errors
        ), f"the refusal did not name the customer: {errors}"

    async def test_the_customer_keeps_the_period_they_paid_for(
        self,
        db_session,
        current_user,
        managed_payment,
        provider,
        provider_env,
        monkeypatch,
        tmp_path,
    ):
        """Switching the tier off must not take the paid period away."""
        config = _config_with(
            tmp_path,
            "plans_config_switched_off.yml",
            lambda source: source["plans"]["managed"].update({"active": False}),
        )
        _install_config(monkeypatch, config)

        await _deliver(db_session, managed_payment.id)

        user = await db_session.get(models.User, current_user.id)
        assert user.plan == "managed"
        assert user.plan_expires_at is not None


# ---------------------------------------------------------------------------
# 5. A provider failure never takes the customer's paid period down.
# ---------------------------------------------------------------------------
class TestAProviderFailureNeverRevokesWhatWasPaid:
    async def test_the_payment_is_still_confirmed(
        self,
        db_session,
        current_user,
        managed_payment,
        provider_env,
        monkeypatch,
        caplog,
    ):
        from api.routes import payments as payments_module

        def _unreachable(*args, **kwargs):
            raise requests.ConnectionError("the payment provider is unreachable")

        monkeypatch.setattr(payments_module.requests, "post", _unreachable)

        with caplog.at_level(logging.ERROR):
            result = await _deliver(db_session, managed_payment.id)

        assert result == {"status": "ok"}, (
            "the provider failure took the payment confirmation down with it"
        )

    async def test_the_customer_keeps_the_thirty_days_they_paid_for(
        self,
        db_session,
        current_user,
        managed_payment,
        provider_env,
        monkeypatch,
        caplog,
    ):
        from api.routes import payments as payments_module

        def _unreachable(*args, **kwargs):
            raise requests.ConnectionError("the payment provider is unreachable")

        monkeypatch.setattr(payments_module.requests, "post", _unreachable)

        with caplog.at_level(logging.ERROR):
            await _deliver(db_session, managed_payment.id)

        user = await db_session.get(models.User, current_user.id)
        assert user.plan == "managed"
        assert user.plan_expires_at is not None
        remaining = _as_utc(user.plan_expires_at) - datetime.now(timezone.utc)
        assert timedelta(days=29) < remaining <= timedelta(days=30)

    async def test_the_failure_is_logged_at_error_with_the_customer_id(
        self,
        db_session,
        current_user,
        managed_payment,
        provider_env,
        monkeypatch,
        caplog,
    ):
        from api.routes import payments as payments_module

        def _unreachable(*args, **kwargs):
            raise requests.ConnectionError("the payment provider is unreachable")

        monkeypatch.setattr(payments_module.requests, "post", _unreachable)

        with caplog.at_level(logging.ERROR):
            await _deliver(db_session, managed_payment.id)

        errors = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.ERROR
        ]
        assert errors, "the failed renewal was not logged at ERROR level"
        assert any(
            "renewal" in message.lower() and str(current_user.id) in message
            for message in errors
        ), f"the failure did not name the customer: {errors}"


# ---------------------------------------------------------------------------
# 6. The price is READ, never written down.
# ---------------------------------------------------------------------------
class TestTheRenewalUsesThePriceThatIsChargedToday:
    async def test_a_repriced_tier_is_renewed_at_the_new_price(
        self,
        db_session,
        current_user,
        managed_payment,
        provider,
        provider_env,
        monkeypatch,
        tmp_path,
    ):
        config = _config_with(
            tmp_path,
            "plans_config_repriced.yml",
            lambda source: source["plans"]["managed"]["billing"]["monthly"].update(
                {"price_usd": 599}
            ),
        )
        _install_config(monkeypatch, config)

        await _deliver(db_session, managed_payment.id)

        assert len(provider) == 1
        assert provider[0]["payload"]["price"] == pytest.approx(599)

        renewal = await crud.get_payment_by_id(
            db_session, provider[0]["payload"]["order_id"]
        )
        assert renewal.amount_usd == pytest.approx(599)


# ---------------------------------------------------------------------------
# 7. The polling fallback renews too: the frontend polls the provider for
#    the invoice status, and a webhook that never arrives must not stop the
#    customer from being invoiced for the next period.
# ---------------------------------------------------------------------------
class TestThePollingFallbackRenewsToo:
    @pytest.fixture
    def paid_invoice(self, monkeypatch):
        """The provider reports the invoice as paid when it is polled."""
        from api.routes import payments as payments_module

        def _get(url, headers=None, timeout=None):
            return _FakeResponse({"id": "inv-0", "status": "complete"})

        monkeypatch.setattr(payments_module.requests, "get", _get)

    async def test_polling_a_paid_invoice_issues_the_renewal(
        self, db_session, current_user, provider, provider_env, paid_invoice
    ):
        from api.routes import payments as payments_module

        payment = await crud.create_payment(
            db_session,
            user_id=current_user.id,
            plan_name="managed",
            amount_usd=plans_config.get_effective_plan_price("managed"),
        )
        await db_session.commit()
        await db_session.refresh(payment)
        await crud.update_payment_with_bitcart_id(
            db_session, payment_id=payment.id, bitcart_id="inv-0"
        )
        await db_session.commit()
        await db_session.refresh(payment)

        result = await payments_module.check_payment_status(
            invoice_id="inv-0",
            db=db_session,
            current_user=current_user,
            redis_client=AsyncMock(),
        )

        assert result["status"] == "completed"
        assert len(provider) == 1, "the polling fallback issued no renewal invoice"

    async def test_polling_twice_issues_one_renewal(
        self, db_session, current_user, provider, provider_env, paid_invoice
    ):
        from api.routes import payments as payments_module

        payment = await crud.create_payment(
            db_session,
            user_id=current_user.id,
            plan_name="managed",
            amount_usd=plans_config.get_effective_plan_price("managed"),
        )
        await db_session.commit()
        await db_session.refresh(payment)
        await crud.update_payment_with_bitcart_id(
            db_session, payment_id=payment.id, bitcart_id="inv-0"
        )
        await db_session.commit()
        await db_session.refresh(payment)

        await payments_module.check_payment_status(
            invoice_id="inv-0",
            db=db_session,
            current_user=current_user,
            redis_client=AsyncMock(),
        )
        await payments_module.check_payment_status(
            invoice_id="inv-0",
            db=db_session,
            current_user=current_user,
            redis_client=AsyncMock(),
        )

        assert len(provider) == 1, "the polling fallback issued a second invoice"


# ---------------------------------------------------------------------------
# 8. The manual purchase route is unchanged: same guards, same payload, same
#    response shape. The renewal reuses that code path rather than a second
#    implementation of it.
# ---------------------------------------------------------------------------
class TestTheManualPurchaseRouteIsUnchanged:
    async def test_the_manual_route_still_returns_the_same_invoice(
        self, db_session, current_user, provider, provider_env
    ):
        from api import schemas
        from api.routes import payments as payments_module

        response = await payments_module.create_payment(
            schemas.CreatePaymentRequest(plan_name="managed"),
            db=db_session,
            current_user=current_user,
        )

        assert set(response) == {
            "invoice_id",
            "invoice_url",
            "payment_address",
            "payment_url",
            "amount",
            "currency",
            "price_usd",
            "expiration_seconds",
            "status",
            "payments",
        }
        assert response["invoice_id"] == "inv-1"
        assert response["price_usd"] == 499

    async def test_the_manual_route_and_the_renewal_use_one_code_path(
        self, db_session, current_user, managed_payment, provider, provider_env
    ):
        """One implementation of "issue an invoice", not two."""
        from api import schemas
        from api.routes import payments as payments_module

        await _deliver(db_session, managed_payment.id)
        manual = await payments_module.create_payment(
            schemas.CreatePaymentRequest(plan_name="managed"),
            db=db_session,
            current_user=current_user,
        )

        assert len(provider) == 2
        renewal_payload = provider[0]["payload"]
        manual_payload = provider[1]["payload"]
        assert set(renewal_payload) == set(manual_payload)
        assert renewal_payload["store_id"] == manual_payload["store_id"]
        assert renewal_payload["notification_url"] == manual_payload["notification_url"]
        assert renewal_payload["redirect_url"] == manual_payload["redirect_url"]
        assert manual["invoice_id"] == "inv-2"
