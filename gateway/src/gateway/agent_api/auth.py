"""The agent layer's two credentials. The maker reads and requests actions; only the Auditor's
token can post a verdict, so a compromised maker cannot forge its own "agree"."""

import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request

from gateway.core.config import Settings


def _caller(request: Request, authorization: str) -> str | None:
    settings: Settings = request.app.state.settings
    tokens = {
        "maker": settings.platform_maker_token.get_secret_value(),
        "auditor": settings.platform_auditor_token.get_secret_value(),
    }
    for name, token in tokens.items():
        if hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
            return name
    return None


def require_maker(request: Request, authorization: str = Header(default="")) -> None:
    caller = _caller(request, authorization)
    if caller is None:
        raise HTTPException(status_code=401, detail="Invalid or missing token.")
    if caller != "maker":
        raise HTTPException(status_code=403, detail="Maker credential required.")


def require_auditor(request: Request, authorization: str = Header(default="")) -> None:
    caller = _caller(request, authorization)
    if caller is None:
        raise HTTPException(status_code=401, detail="Invalid or missing token.")
    if caller != "auditor":
        raise HTTPException(status_code=403, detail="Auditor credential required.")


def require_agent(request: Request, authorization: str = Header(default="")) -> None:
    """Either credential: for read-only routes the Auditor needs too (its own context read, so
    a compromised maker cannot hand it a doctored snapshot)."""
    if _caller(request, authorization) is None:
        raise HTTPException(status_code=401, detail="Invalid or missing token.")


MakerDep = Annotated[None, Depends(require_maker)]
AgentDep = Annotated[None, Depends(require_agent)]
AuditorDep = Annotated[None, Depends(require_auditor)]
