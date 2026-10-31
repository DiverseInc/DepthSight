# tests/test_hub_author_name_is_derived.py
import pytest
from httpx import AsyncClient
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession

from api import models
from api.depthsight_api import app
from api.hub_router import router as hub_router

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def ensure_hub_router_registered():
    """
    Dynamically registers the hub router on the app if it was not loaded on startup
    due to IS_CENTRAL_HUB being False by default.

    Same marker as tests/test_hub_api.py so the router is mounted exactly once
    per test session even though more than one test module needs it.
    """
    if not getattr(app, "_depthsight_hub_router_mounted", False):
        app.include_router(hub_router)
        app._depthsight_hub_router_mounted = True


def make_user(username: str, role: str = "user") -> models.User:
    """A signed-in-able account: get_current_user rejects inactive users."""
    return models.User(
        username=username,
        email=f"{username}@example.com",
        hashed_password="fakehashpassword",
        role=role,
        plan="pro",
        is_active=True,
        xp=0,
        level=1,
    )


async def test_anonymous_topic_post_is_rejected(
    test_client: AsyncClient, db_session: AsyncSession
):
    """
    An anonymous caller must not be able to publish a topic at all, and must
    not be able to choose the displayed author name.
    """
    payload = {
        "topic_type": "discussion",
        "title": "Anonymous Topic",
        "description": "An anonymous caller must not be able to post this.",
        "author_name": "Admin",
    }
    response = await test_client.post("/api/v1/hub/topics", json=payload)
    assert response.status_code == 401

    # Nothing was created.
    result = await db_session.execute(
        select(models.HubTopic).filter(models.HubTopic.title == "Anonymous Topic")
    )
    assert result.scalars().first() is None


async def test_anonymous_topic_comment_is_rejected(
    test_client: AsyncClient,
    db_session: AsyncSession,
    authenticated_client_factory,
):
    """
    An anonymous caller must not be able to comment on a topic.
    """
    owner = make_user("comment_thread_owner")
    db_session.add(owner)
    await db_session.commit()
    await db_session.refresh(owner)

    auth_client = await authenticated_client_factory(owner)
    async with auth_client:
        create_res = await auth_client.post(
            "/api/v1/hub/topics",
            json={
                "topic_type": "discussion",
                "title": "Comment Thread Owner",
                "description": "A topic owned by a signed-in user.",
                "author_name": "comment_thread_owner",
            },
        )
        assert create_res.status_code == 201
        topic_id = create_res.json()["id"]

    response = await test_client.post(
        f"/api/v1/hub/topics/{topic_id}/comments",
        json={"author_name": "Admin", "text": "An anonymous comment."},
    )
    assert response.status_code == 401

    # Nothing was created.
    result = await db_session.execute(
        select(models.HubComment).filter(models.HubComment.topic_id == topic_id)
    )
    assert result.scalars().first() is None


async def test_anonymous_news_comment_is_rejected(
    test_client: AsyncClient, db_session: AsyncSession
):
    """
    An anonymous caller must not be able to comment on a news item.
    """
    news_res = await test_client.get("/api/v1/hub/news")
    assert news_res.status_code == 200
    news_list = news_res.json()
    assert len(news_list) >= 1
    news_id = news_list[0]["id"]

    response = await test_client.post(
        f"/api/v1/hub/news/{news_id}/comments",
        json={"author_name": "Admin", "text": "An anonymous news comment."},
    )
    assert response.status_code == 401

    # Nothing was created.
    result = await db_session.execute(
        select(models.HubNewsComment).filter(models.HubNewsComment.news_id == news_id)
    )
    assert result.scalars().first() is None


async def test_author_name_is_derived_from_the_account(
    test_client: AsyncClient,
    db_session: AsyncSession,
    authenticated_client_factory,
):
    """
    A signed-in user who puts somebody else's name in the body gets their OWN
    name stored and returned -- on a topic, on a topic comment and on a news
    comment. The name in the request body is ignored.
    """
    user = make_user("mallory")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    news_res = await test_client.get("/api/v1/hub/news")
    assert news_res.status_code == 200
    news_id = news_res.json()[0]["id"]

    auth_client = await authenticated_client_factory(user)
    async with auth_client:
        # 1. A topic posted as "Admin".
        topic_res = await auth_client.post(
            "/api/v1/hub/topics",
            json={
                "topic_type": "discussion",
                "title": "Impersonation Attempt",
                "description": "Trying to post under somebody else's name.",
                "author_name": "Admin",
            },
        )
        assert topic_res.status_code == 201
        topic_data = topic_res.json()

        # The RETURNED name is the account's own name, not the one in the body.
        assert topic_data["author_name"] == "mallory"
        assert topic_data["author_name"] != "Admin"

        # And so is the PERSISTED name.
        result = await db_session.execute(
            select(models.HubTopic).filter(models.HubTopic.id == topic_data["id"])
        )
        topic_in_db = result.scalars().first()
        assert topic_in_db is not None
        assert topic_in_db.author_name == "mallory"

        # 2. A topic comment posted as "Admin".
        comment_res = await auth_client.post(
            f"/api/v1/hub/topics/{topic_data['id']}/comments",
            json={"author_name": "Admin", "text": "Impersonating comment."},
        )
        assert comment_res.status_code == 201
        comment_data = comment_res.json()
        assert comment_data["author_name"] == "mallory"
        assert comment_data["author_name"] != "Admin"

        result = await db_session.execute(
            select(models.HubComment).filter(models.HubComment.id == comment_data["id"])
        )
        comment_in_db = result.scalars().first()
        assert comment_in_db is not None
        assert comment_in_db.author_name == "mallory"

        # 3. A news comment posted as "Admin".
        news_comment_res = await auth_client.post(
            f"/api/v1/hub/news/{news_id}/comments",
            json={"author_name": "Admin", "text": "Impersonating news comment."},
        )
        assert news_comment_res.status_code == 201
        news_comment_data = news_comment_res.json()
        assert news_comment_data["author_name"] == "mallory"
        assert news_comment_data["author_name"] != "Admin"

        result = await db_session.execute(
            select(models.HubNewsComment).filter(
                models.HubNewsComment.id == news_comment_data["id"]
            )
        )
        news_comment_in_db = result.scalars().first()
        assert news_comment_in_db is not None
        assert news_comment_in_db.author_name == "mallory"


async def test_admin_post_keeps_the_signed_admin_name(
    test_client: AsyncClient,
    db_session: AsyncSession,
    authenticated_client_factory,
    monkeypatch,
):
    """
    An account whose role is "admin" still shows up as the administrator: the
    stored name is signed and the response is marked is_admin: True.
    """
    from api import hub_router

    monkeypatch.setattr(hub_router, "HUB_ADMIN_API_KEY", "super-secret-admin-key")

    admin = make_user("admin_account", role="admin")
    db_session.add(admin)
    await db_session.commit()
    await db_session.refresh(admin)

    auth_client = await authenticated_client_factory(admin)
    async with auth_client:
        topic_res = await auth_client.post(
            "/api/v1/hub/topics",
            json={
                "topic_type": "discussion",
                "title": "Admin Discussion",
                "description": "A post by the administrator.",
                "author_name": "NotTheAdminName",
            },
        )
        assert topic_res.status_code == 201
        topic_data = topic_res.json()

        assert topic_data["is_admin"] is True
        # The name is the admin account's own name, not the one in the body.
        assert topic_data["author_name"] == "admin_account"
        assert topic_data["author_name"] != "NotTheAdminName"

        # The stored name carries the admin signature.
        result = await db_session.execute(
            select(models.HubTopic).filter(models.HubTopic.id == topic_data["id"])
        )
        topic_in_db = result.scalars().first()
        assert topic_in_db is not None
        assert topic_in_db.author_name != "admin_account"
        assert "[a:" in topic_in_db.author_name

        # The same holds for a comment.
        comment_res = await auth_client.post(
            f"/api/v1/hub/topics/{topic_data['id']}/comments",
            json={"author_name": "NotTheAdminName", "text": "Admin comment."},
        )
        assert comment_res.status_code == 201
        comment_data = comment_res.json()
        assert comment_data["is_admin"] is True
        assert comment_data["author_name"] == "admin_account"

        result = await db_session.execute(
            select(models.HubComment).filter(models.HubComment.id == comment_data["id"])
        )
        comment_in_db = result.scalars().first()
        assert comment_in_db is not None
        assert "[a:" in comment_in_db.author_name


async def test_non_admin_post_is_never_marked_admin(
    test_client: AsyncClient,
    db_session: AsyncSession,
    authenticated_client_factory,
    monkeypatch,
):
    """
    A plain account is never marked as the administrator, even when the hub
    admin key is configured.
    """
    from api import hub_router

    monkeypatch.setattr(hub_router, "HUB_ADMIN_API_KEY", "super-secret-admin-key")

    user = make_user("plain_poster")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    auth_client = await authenticated_client_factory(user)
    async with auth_client:
        topic_res = await auth_client.post(
            "/api/v1/hub/topics",
            json={
                "topic_type": "discussion",
                "title": "Plain Post",
                "description": "A post by an ordinary account.",
                "author_name": "Admin",
            },
        )
        assert topic_res.status_code == 201
        topic_data = topic_res.json()

        assert topic_data["is_admin"] is False
        assert topic_data["author_name"] == "plain_poster"

        result = await db_session.execute(
            select(models.HubTopic).filter(models.HubTopic.id == topic_data["id"])
        )
        topic_in_db = result.scalars().first()
        assert topic_in_db is not None
        assert topic_in_db.author_name == "plain_poster"
        assert "[a:" not in topic_in_db.author_name


async def test_admin_key_post_still_works_but_never_uses_the_body_name(
    test_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
):
    """
    The shared hub admin key still authorises a post (it is how the hub is
    administered), but the name it posts under is derived from the admin
    ACCOUNT -- never from the request body.
    """
    from api import hub_router

    monkeypatch.setattr(hub_router, "HUB_ADMIN_API_KEY", "super-secret-admin-key")

    admin = make_user("key_admin", role="admin")
    db_session.add(admin)
    await db_session.commit()
    await db_session.refresh(admin)

    headers = {"Authorization": "Bearer super-secret-admin-key"}
    response = await test_client.post(
        "/api/v1/hub/topics",
        json={
            "topic_type": "discussion",
            "title": "Key Posted Topic",
            "description": "Posted with the shared admin key.",
            "author_name": "Somebody Else",
        },
        headers=headers,
    )
    assert response.status_code == 201
    topic_data = response.json()

    assert topic_data["is_admin"] is True
    assert topic_data["author_name"] == "key_admin"
    assert topic_data["author_name"] != "Somebody Else"
