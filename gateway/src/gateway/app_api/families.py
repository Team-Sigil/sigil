"""Who am I, and family registration (SPEC §16.1 step 2)."""

from fastapi import APIRouter
from pydantic import BaseModel

from gateway.app_api.sessions import PrincipalDep
from gateway.enums import Role

router = APIRouter(prefix="/api/v1", tags=["families"])


class Me(BaseModel):
    user_id: str
    wallet: str
    role: Role
    family_id: str | None
    display_name: str | None
    locale: str | None


@router.get("/me")
async def me(principal: PrincipalDep) -> Me:
    return Me(
        user_id=str(principal.user.id),
        wallet=principal.user.wallet_address,
        role=principal.role,
        family_id=str(principal.family.id) if principal.family else None,
        display_name=principal.user.display_name,
        locale=principal.user.locale,
    )
