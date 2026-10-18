"""Request size limits and rate limits for the app-facing API (SPEC SEC-11).

Body limits apply to every request and count the bytes actually received, so a missing or
false Content-Length does not get around them. Rate limits apply to `/api/v1/*` per bearer
token, or per client address before sign-in. Both are in-process (one gateway instance).
"""

import hashlib
import re
import time
from collections import defaultdict

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gateway.core.errors import error_response

DEFAULT_BODY_LIMIT = 64 * 1024
AGENT_BODY_LIMIT = 1024 * 1024
PROOF_BODY_LIMIT = 6 * 1024 * 1024  # a 5 MB proof plus multipart overhead
PROOF_PATH = re.compile(r"^/api/v1/requests/[^/]+/proofs$")


def body_limit(path: str) -> int:
    if PROOF_PATH.match(path):
        return PROOF_BODY_LIMIT
    if path.startswith("/agent/v1/"):
        return AGENT_BODY_LIMIT
    return DEFAULT_BODY_LIMIT


class BodyLimitMiddleware:
    """Reads the body up to its limit before the app sees it, then replays it. Memory per
    request is bounded by the limit."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = body_limit(scope["path"])
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > limit:
            await self._refuse(scope, receive, send)
            return

        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > limit:
                await self._refuse(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        body = b"".join(chunks)
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()  # later reads only see disconnects

        await self.app(scope, replay, send)

    @staticmethod
    async def _refuse(scope: Scope, receive: Receive, send: Send) -> None:
        response = error_response(413, "PAYLOAD_TOO_LARGE", "Request body is too large.")
        await response(scope, receive, send)


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp, per_minute: int) -> None:
        self.app = app
        self.per_minute = per_minute
        self._counts: dict[tuple[str, int], int] = defaultdict(int)

    def _key(self, scope: Scope) -> str:
        authorization = dict(scope["headers"]).get(b"authorization", b"")
        if authorization:
            # Hashed so tokens are never held in memory longer than needed.
            return "token:" + hashlib.sha256(authorization).hexdigest()
        client = scope.get("client")
        return "ip:" + (client[0] if client else "unknown")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/v1/"):
            await self.app(scope, receive, send)
            return
        window = int(time.time() // 60)
        key = (self._key(scope), window)
        # Forget past windows so memory stays bounded.
        for old in [k for k in self._counts if k[1] < window]:
            del self._counts[old]
        self._counts[key] += 1
        if self._counts[key] > self.per_minute:
            response = error_response(429, "RATE_LIMITED", "Too many requests; slow down.")
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
