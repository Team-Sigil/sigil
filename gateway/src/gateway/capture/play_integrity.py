"""Play Integrity: Google confirms the request came from our genuine app, installed from Play,
on a device that passes integrity checks.

The app requests a classic integrity token with `nonce` set to the enrolment challenge; the
gateway decodes it with Google's `decodeIntegrityToken` API using a service account. Without a
service account, every token is refused (fail closed).
"""

import json
import time
from pathlib import Path
from typing import Any, Protocol

import httpx
import jwt

from gateway.capture.errors import AttestationError

SCOPE = "https://www.googleapis.com/auth/playintegrity"
DECODE_URL = "https://playintegrity.googleapis.com/v1/{package}:decodeIntegrityToken"
MAX_TOKEN_AGE_MS = 10 * 60 * 1000
MAX_CLOCK_SKEW_MS = 60 * 1000  # Google's clock and ours never agree exactly
DEVICE_VERDICTS = {"MEETS_DEVICE_INTEGRITY", "MEETS_STRONG_INTEGRITY"}


class IntegrityVerifier(Protocol):
    async def verify(self, token: str, nonce: str, now_ms: int) -> None: ...


class NoIntegrityVerifier:
    async def verify(self, token: str, nonce: str, now_ms: int) -> None:
        raise AttestationError("Play Integrity is not configured")


def check_verdict(payload: dict[str, Any], package: str, nonce: str, now_ms: int) -> None:
    """The decoded token must be fresh, for this enrolment, from our Play-recognised app on a
    device that meets integrity."""
    request = payload.get("requestDetails") or {}
    app = payload.get("appIntegrity") or {}
    device = payload.get("deviceIntegrity") or {}
    if request.get("requestPackageName") != package or app.get("packageName") != package:
        raise AttestationError("integrity token is for another app")
    if request.get("nonce") != nonce:
        raise AttestationError("integrity token is for another challenge")
    try:
        age = now_ms - int(request.get("timestampMillis", 0))
    except (TypeError, ValueError) as exc:
        raise AttestationError("integrity token has no timestamp") from exc
    if not -MAX_CLOCK_SKEW_MS <= age <= MAX_TOKEN_AGE_MS:
        raise AttestationError("integrity token is stale")
    if app.get("appRecognitionVerdict") != "PLAY_RECOGNIZED":
        raise AttestationError("app is not recognised by Play")
    if not DEVICE_VERDICTS & set(device.get("deviceRecognitionVerdict") or []):
        raise AttestationError("device does not meet integrity")


class GooglePlayIntegrity:
    def __init__(
        self,
        package: str,
        service_account: dict[str, Any],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._package = package
        self._account = service_account
        self._http = httpx.AsyncClient(timeout=15.0, transport=transport, follow_redirects=False)
        self._token: tuple[str, float] | None = None

    @classmethod
    def from_file(cls, package: str, path: str) -> "GooglePlayIntegrity":
        return cls(package, json.loads(Path(path).read_text()))

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _access_token(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        now = int(time.time())
        token_uri = self._account.get("token_uri", "https://oauth2.googleapis.com/token")
        assertion = jwt.encode(
            {
                "iss": self._account["client_email"],
                "scope": SCOPE,
                "aud": token_uri,
                "iat": now,
                "exp": now + 3600,
            },
            self._account["private_key"],
            algorithm="RS256",
        )
        response = await self._http.post(
            token_uri,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
        if response.status_code != 200:
            raise AttestationError(f"Google token exchange failed ({response.status_code})")
        body = response.json()
        self._token = (body["access_token"], now + int(body.get("expires_in", 3600)))
        return self._token[0]

    async def verify(self, token: str, nonce: str, now_ms: int) -> None:
        try:
            access = await self._access_token()
            response = await self._http.post(
                DECODE_URL.format(package=self._package),
                json={"integrity_token": token},
                headers={"Authorization": f"Bearer {access}"},
            )
        except (httpx.HTTPError, jwt.PyJWTError, KeyError, ValueError) as exc:
            raise AttestationError(f"Play Integrity unavailable: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise AttestationError(f"Play Integrity refused the token ({response.status_code})")
        try:
            payload = response.json()["tokenPayloadExternal"]
        except (ValueError, KeyError) as exc:
            raise AttestationError("Play Integrity response outside its contract") from exc
        check_verdict(payload, self._package, nonce, now_ms)
