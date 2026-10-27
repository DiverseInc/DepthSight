# api/main.py
"""Request-level guards for the DepthSight API application.

The guards in this module have to run BEFORE a request body is parsed.

Starlette parses a multipart body and spools it to a temporary file on disk
(past 1 MB) while the endpoint handler has not run yet. A size check inside a
handler is therefore NOT a request-size limit: by the time the handler runs,
the bytes are already on disk. The middleware below reads the body itself, up
to the cap, and refuses the request with a 413 before the application - and
therefore before Starlette's parser - ever sees it.

Reading the body here also means the cap does not depend on the endpoint
parsing it: a route that ignores its body would otherwise be able to stream an
unbounded body past the guard.

It counts what actually arrives instead of trusting Content-Length, so a
chunked request with no (or a lying) Content-Length is bounded too.
"""

import json
import logging
import os
from typing import Optional

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

# Generous enough for every legitimate body the API accepts: the largest is
# the 5 MB strategy upload (plus multipart overhead), and a support ticket
# message carries a base64 screenshot that the frontend caps at 5 MB.
# Small enough that a hostile upload cannot fill the disk or the memory of a
# worker.
DEFAULT_MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024  # 16 MB


def get_max_request_body_bytes() -> int:
    """The request body cap in bytes, overridable with MAX_REQUEST_BODY_BYTES."""
    raw = os.getenv("MAX_REQUEST_BODY_BYTES")
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
        logger.warning(
            "MAX_REQUEST_BODY_BYTES=%r is not a positive integer; using the default.",
            raw,
        )
    return DEFAULT_MAX_REQUEST_BODY_BYTES


class BodySizeLimitMiddleware:
    """Pure-ASGI middleware that bounds the size of an incoming request body.

    The cap is applied globally rather than to one route. A single oversized
    multipart body is enough to fill a disk, and a global cap also covers any
    endpoint that is added later, so it cannot be bypassed by picking a
    different route.
    """

    def __init__(self, app: ASGIApp, max_bytes: Optional[int] = None) -> None:
        self.app = app
        # None means "resolve MAX_REQUEST_BODY_BYTES per request", so the cap
        # can be changed without a restart and so it can be exercised by a
        # test against the real application.
        self.max_bytes = max_bytes

    @property
    def effective_max_bytes(self) -> int:
        if self.max_bytes is not None:
            return self.max_bytes
        return get_max_request_body_bytes()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        max_bytes = self.effective_max_bytes

        # Fast path: a declared Content-Length over the cap is refused
        # without reading a single byte of the body.
        declared = self._declared_content_length(scope)
        if declared is not None and declared > max_bytes:
            await self._send_too_large(send, max_bytes)
            return

        # Read the body ourselves, stopping at the cap. Stopping early is the
        # point: the rest of the body is never received, so it is never
        # parsed, spooled to disk or buffered by the application.
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            body.extend(message.get("body", b""))
            if len(body) > max_bytes:
                await self._send_too_large(send, max_bytes)
                return
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if replayed:
                # Once the body has been delivered the ASGI contract is that
                # the next receive is a disconnect. Starlette's
                # BaseHTTPMiddleware enforces this: it raises
                # "Unexpected message received" if it gets anything else.
                return {"type": "http.disconnect"}
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay_receive, send)

    @staticmethod
    def _declared_content_length(scope: Scope) -> Optional[int]:
        raw = Headers(scope=scope).get("content-length")
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    async def _send_too_large(self, send: Send, max_bytes: int) -> None:
        body = json.dumps(
            {"detail": f"Request body exceeds the {max_bytes} byte limit."}
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
