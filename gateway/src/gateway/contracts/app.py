"""Field types shared by the app-facing API (see README "App API")."""

from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, Field

from gateway.core.crypto import is_valid_pubkey


def _check_pubkey(value: str) -> str:
    if not is_valid_pubkey(value):
        raise ValueError("not a valid Solana public key")
    return value


def _check_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("unknown timezone") from exc
    return value


Pubkey = Annotated[str, AfterValidator(_check_pubkey)]
Timezone = Annotated[str, AfterValidator(_check_timezone)]
SignatureB58 = Annotated[str, Field(min_length=1, max_length=128)]
NonceValue = Annotated[str, Field(min_length=1, max_length=64)]
# Money from the app: integer base units only (no floats, strings or bools).
Units = Annotated[int, Field(strict=True, gt=0)]
