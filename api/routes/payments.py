import logging
import os
import requests
from typing import Any, Dict, Optional

import redis.asyncio as redis
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from .. import crud, models, schemas
from ..auth import get_current_user
from ..database import get_db
from ..redis_client import get_redis_client
from ..plans import build_plan_catalog, plans_config

logger = logging.getLogger(__name__)

# --- Payments Router ---
payments_router = APIRouter(
    prefix="/api/v1/payments",
    tags=["Payments"],
    dependencies=[Depends(get_current_user)],
)


@payments_router.get("/plans")
async def get_plans(db: AsyncSession = Depends(get_db)):
    """
    Get a list of all available plans from the config file.

    Built from the SAME `build_plan_catalog()` the public marketing pricing
    page uses, so the in-app billing page and the public page can never show
    different entitlements for the same tier.
    """
    try:
        from ..depthsight_api import _get_lifetime_slots_for_plan

        catalog = build_plan_catalog()
        billing_mode = catalog["billing_mode"]
        all_plans = plans_config.get_all_plans()
        # Convert to required format
        response_plans = []
        for plan in catalog["plans"]:
            key = plan["key"]
            config = all_plans.get(key, {})
            plan_billing = plans_config.get_plan_billing(key, billing_mode)
            # A tier the catalog does not sell in this billing mode is not
            # OFFERED. This is the SAME rule the purchase endpoint enforces
            # (`plans_config.is_billing_mode_available`), so the billing page
            # can never offer a tier the server will refuse: the $499 Managed
            # tier is a monthly service and its one-off block is switched
            # off (`billing.lifetime.enabled: false`).
            if not plans_config.is_billing_mode_available(key, billing_mode):
                continue
            is_lifetime_offer = billing_mode == "lifetime" and plan_billing.get(
                "enabled", False
            )
            slots = (
                await _get_lifetime_slots_for_plan(db, key, config)
                if is_lifetime_offer
                else None
            )
            response_plans.append(
                {
                    "key": key,
                    "name": plan["name"],
                    "price_usd": plan["price_usd"],
                    "active": plan["active"],
                    "billing_mode": billing_mode,
                    "period_label": "lifetime" if is_lifetime_offer else "month",
                    "slots": slots,
                    "description": plan["description"],
                    "features": plan["features"],
                    # Derived from the real quotas/permissions/limits. The
                    # marketing page renders these, not the hand-written copy.
                    "entitlements": plan["entitlements"],
                    "billing": plan["billing"],
                }
            )
        return response_plans
    except Exception as e:
        logger.error(f"Failed to load plans from config: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail="Could not load plans configuration."
        )


# The tiers that renew themselves (owner decision, 2026-11-06): the $499
# Managed service is issued its next 30-day invoice automatically, so the
# customer always has the next invoice in front of them, nobody has to
# invoice by hand, and the service continues for exactly as long as the
# customer keeps paying. EVERY OTHER TIER KEEPS ITS ONE-OFF BEHAVIOUR --
# the owner approved auto-renew for the Managed service only.
AUTO_RENEW_PLANS = ("managed",)


async def issue_plan_invoice(
    db: AsyncSession,
    *,
    user_id: int,
    plan_name: str,
    buyer_email: str,
) -> Dict[str, Any]:
    """Create the payment row AND the provider invoice for ONE period.

    This is the SINGLE place a Bitcart invoice is created. The manual route
    (`POST /payments/create`) and the Managed auto-renewal both call it, so
    the guards, the price and the provider payload can never drift apart:

      * the tier must still be ACTIVE,
      * the tier must still be SOLD in the active billing mode,
      * the price is the CURRENT effective price,
      * the provider payload (price, store, order_id, notification_url,
        redirect_url, buyer_email) is exactly the one the customer already
        knows.

    `order_id` stays `str(payment.id)`, so the provider's webhook resolves
    the invoice back to this payment row, and therefore to this customer.

    Raises HTTPException when the tier may not be sold, or when the payment
    provider could not be reached.
    """
    from ..depthsight_api import _get_lifetime_slots_for_plan

    plan_config = plans_config.get_plan(plan_name)

    if not plan_config or not plan_config.get("active", False):
        raise HTTPException(status_code=400, detail="Plan not found or is not active.")

    billing_mode = plans_config.get_billing_mode()

    # The catalog declares, per billing mode, whether a tier is sold at all
    # (`billing.<mode>.enabled`). A mode that is switched off must never be
    # purchasable: `get_effective_plan_price` falls back to the MONTHLY
    # price when the mode carries no price of its own, so a purchase in the
    # switched-off mode would create a ONE-OFF invoice for a tier that has
    # no one-off price, and the customer would be granted the monthly
    # period for it. The $499 Managed tier is the case in point: it is a
    # monthly service and its one-off block is switched off, so it must not
    # be sold as a one-off at all.
    if not plans_config.is_billing_mode_available(plan_name, billing_mode):
        raise HTTPException(
            status_code=400,
            detail=(
                f"The {plan_config.get('name', plan_name)} plan is not "
                f"available for {billing_mode} billing."
            ),
        )

    price_usd = plans_config.get_effective_plan_price(plan_name)
    if billing_mode == "lifetime":
        lifetime_slots = await _get_lifetime_slots_for_plan(db, plan_name, plan_config)
        if lifetime_slots and lifetime_slots["available"] <= 0:
            raise HTTPException(
                status_code=409, detail="No lifetime seats available for this plan."
            )

    # 1. Create a payment record in our DB
    payment = await crud.create_payment(
        db, user_id=user_id, plan_name=plan_name, amount_usd=price_usd
    )
    await db.commit()
    await db.refresh(payment)

    # 2. Request to Bitcart
    try:
        bitcart_api_url = os.getenv(
            "BITCART_API_URL"
        )  # e.g. https://depthsight.diverseinc.net/bitcart-api
        bitcart_api_key = os.getenv("BITCART_API_KEY")
        bitcart_store_id = os.getenv("BITCART_STORE_ID")
        frontend_url = os.getenv("FRONTEND_BASE_URL", "http://localhost:5173")

        if not bitcart_api_url or not bitcart_api_key or not bitcart_store_id:
            logger.error(
                "Bitcart configuration (URL, API_KEY, or STORE_ID) is missing."
            )
            raise ValueError("Payment provider is not configured.")

        headers = {
            "Authorization": f"Bearer {bitcart_api_key}",
            "Content-Type": "application/json",
        }

        # Use the actual external API URL for webhooks so Bitcart can reach us
        api_base_url = os.getenv("API_BASE_URL", "https://depthsight.diverseinc.net/api/v1")

        payload = {
            "price": float(price_usd),
            "store_id": bitcart_store_id,
            "order_id": str(payment.id),
            "notification_url": f"{api_base_url}/webhooks/bitcart",
            "redirect_url": f"{frontend_url}/account?payment=success",
            "buyer_email": buyer_email,
        }

        logger.info(
            f"Creating Bitcart invoice for payment {payment.id} with payload: {payload}"
        )

        # Bitcart endpoint to create an invoice
        response = requests.post(
            f"{bitcart_api_url.rstrip('/')}/invoices",
            headers=headers,
            json=payload,
            timeout=60,  # Increased timeout for address generation
        )

        if response.status_code >= 400:
            logger.error(
                f"Bitcart API Error. Status: {response.status_code}. Response: {response.text}"
            )

        response.raise_for_status()

        data = response.json()
        bitcart_invoice_id = data.get("id")

        if not bitcart_invoice_id:
            logger.error(f"Could not find 'id' in Bitcart response: {data}")
            raise HTTPException(
                status_code=500,
                detail="Failed to parse response from payment provider.",
            )

        await crud.update_payment_with_bitcart_id(
            db, payment_id=payment.id, bitcart_id=str(bitcart_invoice_id)
        )
        await db.commit()

        # Extract payment details from the Bitcart invoice response
        payments = data.get("payments", [])
        payment_address = None
        payment_url = None  # bitcoin: URI for QR code
        amount_crypto = None
        currency_crypto = None

        if payments:
            first_payment = payments[0]
            payment_address = first_payment.get("payment_address")
            payment_url = first_payment.get("payment_url")
            amount_crypto = first_payment.get("amount")
            currency_crypto = first_payment.get("currency", "BTC")

        return {
            "payment_id": payment.id,
            "invoice": {
                "invoice_id": bitcart_invoice_id,
                "invoice_url": f"{bitcart_api_url.rstrip('/')}/invoices/{bitcart_invoice_id}",
                "payment_address": payment_address,
                "payment_url": payment_url,
                "amount": amount_crypto,
                "currency": currency_crypto,
                "price_usd": float(price_usd),
                "expiration_seconds": data.get("expiration_seconds", 900),
                "status": data.get("status", "pending"),
                "payments": data.get("payments", []),
            },
        }

    except requests.RequestException as e:
        logger.error(f"Bitcart API request failed: {e}")
        await crud.update_payment_status(db, payment_id=payment.id, status="FAILED")
        await db.commit()
        raise HTTPException(
            status_code=502, detail="Failed to communicate with payment provider."
        )

    except Exception as e:
        logger.error(f"Error creating payment via Bitcart: {e}", exc_info=True)
        await crud.update_payment_status(db, payment_id=payment.id, status="FAILED")
        await db.commit()
        raise HTTPException(status_code=500, detail="An internal error occurred.")


async def issue_renewal_invoice(
    db: AsyncSession,
    *,
    payment: models.Payment,
    user: models.User,
) -> Optional[Dict[str, Any]]:
    """Issue the NEXT period's invoice for a customer whose tier renews.

    The chain the owner approved (2026-11-06): invoice N is paid -> the
    plan extends by its period -> invoice N+1 is issued -> invoice N+1 is
    paid -> ... The customer always has the next invoice in front of them,
    nobody invoices by hand, and the service continues for exactly as long
    as the customer keeps paying.

    NOTHING HERE RAISES. A renewal that cannot be issued is logged at
    ERROR with the customer id and the reason, and the customer keeps the
    period they just paid for: a failed renewal must never take the
    payment confirmation down with it, and must never take away what the
    customer paid for.

    Returns the invoice that was issued, or None when no invoice was issued
    (the tier does not renew, an invoice for the next period is already
    open, or the tier may no longer be sold -- which is LOGGED, never
    swallowed).
    """
    try:
        plan_key = plans_config.resolve_plan_key(payment.plan_name)
        if plan_key not in AUTO_RENEW_PLANS:
            logger.info(
                "Payment %s is for tier '%s', which does not renew automatically; "
                "no renewal invoice issued.",
                payment.id,
                payment.plan_name,
            )
            return None

        # A customer who bought a LIFETIME seat is not on a monthly cycle:
        # there is nothing to renew, and a renewal would bill them monthly
        # for a seat they already own.
        from ..depthsight_api import _is_lifetime_payment

        if _is_lifetime_payment(payment):
            logger.info(
                "Payment %s is a lifetime payment; no renewal invoice issued.",
                payment.id,
            )
            return None

        # Serialise the renewals for this customer: two of them that run at
        # the same time must not both see "no open invoice" and both create
        # one. The lock is held until the invoice row is committed.
        await crud.lock_customer_billing(db, user_id=user.id)

        # Idempotency: there must never be two OPEN invoices for the same
        # customer and the same tier. A replayed confirmation finds the
        # invoice the first one created and stops.
        open_payment = await crud.get_open_payment_for_plan(
            db, user_id=user.id, plan_name=payment.plan_name
        )
        if open_payment is not None:
            logger.info(
                "Customer %s already has an open invoice (%s) for tier '%s'; "
                "no second renewal invoice issued.",
                user.id,
                open_payment.id,
                payment.plan_name,
            )
            return None

        result = await issue_plan_invoice(
            db,
            user_id=user.id,
            plan_name=payment.plan_name,
            buyer_email=user.email,
        )
        logger.info(
            "Issued the renewal invoice %s (payment %s) for customer %s on tier '%s'.",
            result["invoice"]["invoice_id"],
            result["payment_id"],
            user.id,
            payment.plan_name,
        )
        return result["invoice"]
    except HTTPException as exc:
        # The tier may no longer be sold (it was switched off), or the
        # provider could not be reached. Either way this must be VISIBLE: a
        # customer who is NOT invoiced has to be findable in the logs.
        logger.error(
            "Failed to issue the renewal invoice for customer %s on tier '%s': %s",
            user.id,
            payment.plan_name,
            exc.detail,
        )
        return None
    except Exception as exc:
        logger.error(
            "Failed to issue the renewal invoice for customer %s on tier '%s': %s",
            user.id,
            payment.plan_name,
            exc,
            exc_info=True,
        )
        return None


@payments_router.post("/create")
async def create_payment(
    request: schemas.CreatePaymentRequest,
    db: AsyncSession = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Creates a payment invoice via Bitcart.

    The invoice itself is created by `issue_plan_invoice`, which the
    Managed auto-renewal calls too: one implementation, one set of guards,
    one price rule, one provider payload.
    """
    result = await issue_plan_invoice(
        db,
        user_id=current_user.id,
        plan_name=request.plan_name,
        buyer_email=current_user.email,
    )
    return result["invoice"]


@payments_router.get("/check/{invoice_id}")
async def check_payment_status(
    invoice_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
    redis_client: redis.Redis = Depends(get_redis_client),
):
    """
    Manually checks payment status with Bitcart and activates subscription if paid.
    Used for frontend polling to avoid relying on webhooks.
    """
    try:
        from ..depthsight_api import (
            _get_payment_plan_expires_at,
            _sync_live_runtime_for_plan_change,
        )

        bitcart_api_url = os.getenv("BITCART_API_URL", "http://bitcart_api:8000")
        bitcart_api_key = os.getenv("BITCART_API_KEY")

        headers = {}
        if bitcart_api_key:
            headers["Authorization"] = f"Bearer {bitcart_api_key}"

        # Get invoice details from Bitcart
        response = requests.get(
            f"{bitcart_api_url.rstrip('/')}/invoices/{invoice_id}",
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()

        bitcart_status = data.get("status")
        normalized_status = str(bitcart_status or "").strip().lower()
        logger.info(f"Checking status for invoice {invoice_id}: {bitcart_status}")

        # If paid, trigger the same logic as webhook would
        if normalized_status in {
            "complete",
            "completed",
            "confirmed",
            "finished",
            "paid",
        }:
            # Find the payment in our DB
            payment = await crud.get_payment_by_bitcart_id(db, bitcart_id=invoice_id)
            if not payment:
                logger.warning(
                    f"Bitcart polling for unknown invoice_id '{invoice_id}'."
                )
                raise HTTPException(status_code=404, detail="Payment not found.")
            if payment.user_id != current_user.id:
                logger.warning(
                    "User %s attempted to poll invoice %s owned by user %s.",
                    current_user.id,
                    invoice_id,
                    payment.user_id,
                )
                raise HTTPException(
                    status_code=403, detail="Payment does not belong to current user."
                )

            if payment.status not in {"FINISHED", "COMPLETED"}:
                # Update status and activate subscription
                await crud.update_payment_status(
                    db, payment_id=payment.id, status="FINISHED"
                )

                # Activate plan
                user = await db.get(models.User, payment.user_id)
                if not user:
                    logger.error(
                        f"User with id {payment.user_id} not found for successful payment {payment.id}"
                    )
                    raise HTTPException(status_code=404, detail="User not found.")

                previous_plan = user.plan
                await crud.update_user_plan(
                    db,
                    user_id=user.id,
                    plan_name=payment.plan_name,
                    expires_at=_get_payment_plan_expires_at(payment),
                )
                await crud.create_commission_for_payment(db, payment)
                await db.commit()

                try:
                    await _sync_live_runtime_for_plan_change(
                        redis_client=redis_client,
                        db=db,
                        user_id=user.id,
                        previous_plan=previous_plan,
                        new_plan=payment.plan_name,
                    )
                except Exception as exc:
                    logger.error(
                        "Failed to sync live runtime after payment polling for user_id=%s: %s",
                        user.id,
                        exc,
                        exc_info=True,
                    )

                # --- AUTO-RENEWAL (owner decision, 2026-11-06) ---
                # The same renewal the webhook issues, on the same terms:
                # the period the customer just paid for is already recorded
                # and committed, and a renewal that cannot be issued must
                # never take the confirmation -- or the service they paid
                # for -- down with it.
                try:
                    await issue_renewal_invoice(db, payment=payment, user=user)
                except Exception as exc:
                    logger.error(
                        "Failed to run the auto-renewal for user_id=%s after payment %s: %s",
                        user.id,
                        payment.id,
                        exc,
                        exc_info=True,
                    )

                return {"status": "completed", "message": "Subscription activated!"}

            return {"status": "completed", "message": "Already processed."}

        if normalized_status in {
            "expired",
            "invalid",
            "failed",
            "cancelled",
            "canceled",
        }:
            payment = await crud.get_payment_by_bitcart_id(db, bitcart_id=invoice_id)
            if (
                payment
                and payment.status == "PENDING"
                and payment.user_id == current_user.id
            ):
                terminal_status = (
                    "EXPIRED" if normalized_status == "expired" else "FAILED"
                )
                await crud.update_payment_status(
                    db, payment_id=payment.id, status=terminal_status
                )
                await db.commit()

        return {
            "status": normalized_status or "unknown",
            "message": f"Payment is {bitcart_status}",
        }

    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Error checking payment status: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}
