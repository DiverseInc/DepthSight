# tests/test_registration_plan_entitlement.py
"""FIX 1 -- a signup may only start on a tier it is actually entitled to.

The pricing page linked to `/register?plan=pro`, but:
  * the frontend read only `?ref=`, so the plan was silently dropped, and
  * the backend ignored plans entirely: with `registration_trial.days == 0`
    a new account was always created on the `free` plan.

The plan is now forwarded, and the server honours it ONLY for the tier the
signup is entitled to (the registration trial tier). A paid tier is never
granted at signup.
"""

import pytest
from sqlalchemy import func, select

from api import crud, models, schemas


async def _ensure_a_user_exists(db):
    """The very first account on a fresh install gets `pro` forever.

    That is unrelated to this feature, so the tests start from a database
    that already has at least one user in it.
    """
    count = (await db.execute(select(func.count(models.User.id)))).scalar() or 0
    if count == 0:
        db.add(
            models.User(
                username="baseline-user",
                email="baseline@example.net",
                hashed_password="x",
                plan="free",
            )
        )
        await db.commit()


def _user_create(username="new-signup", plan=None):
    return schemas.UserCreate(
        username=username,
        email=f"{username}@example.net",
        password="Str0ng-Passw0rd!",
        plan=plan,
    )


class TestRegistrationPlanEntitlement:
    async def test_a_requested_paid_tier_is_not_granted(self, db_session):
        await _ensure_a_user_exists(db_session)

        user = await crud.create_user(
            db=db_session, user=_user_create("wants-pro", plan="pro")
        )

        assert user.plan == "free", (
            "A signup must never be granted a paid tier."
        )
        assert user.plan_expires_at is None

    async def test_the_trial_tier_is_not_granted_while_the_trial_is_off(
        self, db_session
    ):
        await _ensure_a_user_exists(db_session)

        user = await crud.create_user(
            db=db_session, user=_user_create("wants-standard", plan="standard")
        )

        # registration_trial.days is 0, so there is no trial to be on.
        assert user.plan == "free"

    async def test_the_trial_tier_is_granted_when_a_trial_really_runs(
        self, db_session, mocker
    ):
        await _ensure_a_user_exists(db_session)
        mocker.patch(
            "api.crud.plans_config.get_registration_trial_config",
            return_value={"enabled": True, "plan": "standard", "days": 14},
        )

        user = await crud.create_user(
            db=db_session, user=_user_create("trial-user", plan="standard")
        )

        assert user.plan == "standard"
        assert user.plan_expires_at is not None, "A trial must expire."

    async def test_a_paid_tier_is_still_refused_while_a_trial_runs(
        self, db_session, mocker
    ):
        """A running trial grants the TRIAL tier, not any paid tier."""
        await _ensure_a_user_exists(db_session)
        mocker.patch(
            "api.crud.plans_config.get_registration_trial_config",
            return_value={"enabled": True, "plan": "standard", "days": 14},
        )

        user = await crud.create_user(
            db=db_session, user=_user_create("wants-pro-anyway", plan="pro")
        )

        assert user.plan == "standard"
        assert user.plan_expires_at is not None

    async def test_no_requested_plan_leaves_the_account_on_free(self, db_session):
        await _ensure_a_user_exists(db_session)

        user = await crud.create_user(db=db_session, user=_user_create("plain"))

        assert user.plan == "free"


class TestRegisterEndpointRejectsUnknownPlans:
    async def test_an_unknown_plan_is_a_400(self, test_client):
        response = await test_client.post(
            "/api/v1/register",
            json={
                "username": "unknown-plan-user",
                "email": "unknown-plan-user@example.net",
                "password": "Str0ng-Passw0rd!",
                "plan": "enterprise",
            },
        )
        assert response.status_code == 400, response.text
