"""Tests for the ASGI request body size limit.

Starlette parses a multipart body and spools it to a temporary file on disk
(past 1 MB) BEFORE the endpoint handler runs. A size check inside a handler is
therefore NOT a request-size limit: by the time the handler runs, the bytes
are already on disk. The middleware in api/main.py counts the bytes as they
stream in and aborts the request as soon as the cap is passed, so an oversized
body is never fully received.
"""

import pytest

from api.main import BodySizeLimitMiddleware

pytestmark = pytest.mark.asyncio


async def _run_middleware(middleware, scope, chunks):
    """Drive a middleware with a stub app and return (statuses, bytes_seen)."""
    statuses = []
    seen = bytearray()
    called = False

    async def stub_app(scope, receive, send):
        nonlocal called
        called = True
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            seen.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(message):
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    pending = list(chunks)
    delivered = bytearray()

    async def receive():
        nonlocal pending
        if pending:
            chunk = pending.pop(0)
            delivered.extend(chunk)
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": bool(pending),
            }
        return {"type": "http.request", "body": b"", "more_body": False}

    middleware.app = stub_app
    await middleware(scope, receive, send)
    return statuses, bytes(seen), bytes(delivered), called


async def test_middleware_refuses_the_body_before_the_app_is_called():
    """The downstream app must never be handed an oversized body."""
    middleware = BodySizeLimitMiddleware(None, max_bytes=1000)
    chunks = [b"x" * 500 for _ in range(10)]  # 5000 bytes in total

    statuses, seen, delivered, called = await _run_middleware(
        middleware, {"type": "http", "headers": []}, chunks
    )

    assert statuses == [413]
    # The application was never called, so it never saw the body.
    assert called is False
    assert seen == b""
    # The source was not drained either: the read stopped at the cap.
    assert len(delivered) <= 1500


async def test_middleware_refuses_a_declared_oversized_body_without_reading_it():
    """A Content-Length over the cap is refused before the body is read."""
    statuses = []
    read_called = False

    async def send(message):
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    async def receive():  # pragma: no cover - must never be called
        nonlocal read_called
        read_called = True
        return {"type": "http.request", "body": b"", "more_body": False}

    middleware = BodySizeLimitMiddleware(None, max_bytes=1000)
    scope = {"type": "http", "headers": [(b"content-length", b"5000")]}

    await middleware(scope, receive, send)

    assert statuses == [413]
    assert read_called is False


async def test_middleware_passes_a_body_under_the_cap_to_the_app():
    middleware = BodySizeLimitMiddleware(None, max_bytes=1000)

    statuses, seen, _delivered, called = await _run_middleware(
        middleware, {"type": "http", "headers": []}, [b"x" * 100, b"x" * 100]
    )

    assert statuses == [200]
    assert called is True
    assert seen == b"x" * 200


async def test_upload_strategy_refuses_an_oversized_body_at_ingress(
    authenticated_client, monkeypatch
):
    """The ingress bound rejects the body - not the in-handler 5 MB check.

    This is the case that matters: a multipart body is what Starlette spools
    to disk before the handler runs.

    The cap is set low so the test does not have to push 32 MB through the
    socket; the middleware resolves MAX_REQUEST_BODY_BYTES per request.
    """
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "1024")

    response = await authenticated_client.post(
        "/api/simulation/upload-strategy",
        files={
            "file": ("strategy.json", b"x" * 4096, "application/json"),
        },
    )

    assert response.status_code == 413, response.text
    # The ingress message, not the handler's "max 5MB" message.
    assert "1024 byte limit" in response.text
    assert "max 5MB" not in response.text


async def test_a_chunked_body_is_refused_at_ingress(test_client, monkeypatch):
    """A body with no Content-Length is bounded while it streams in.

    A JSON body is used because Starlette only reads the body of a request
    whose content type it understands; a multipart body is read (and spooled)
    and a JSON body is read, but an opaque content type is never read at all.
    """
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "1024")

    async def body():
        for _ in range(8):
            yield b'{"padding":"' + b"x" * 500 + b'"}'

    response = await test_client.post(
        "/api/simulation/backtest",
        content=body(),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413, response.text
    assert "1024 byte limit" in response.text


async def test_upload_strategy_still_accepts_a_body_under_the_ingress_cap(
    authenticated_client, monkeypatch
):
    """The ingress limit must not break the normal upload path."""
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", str(5 * 1024 * 1024))

    response = await authenticated_client.post(
        "/api/simulation/upload-strategy",
        files={
            "file": (
                "strategy.json",
                b'{"strategy_name": "small"}',
                "application/json",
            ),
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["valid"] is True
