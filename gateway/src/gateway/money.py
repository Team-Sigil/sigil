"""Conversions between on-chain base units and the display units the model sees.

The SPL token has 6 decimals. The model only ever reads and writes display amounts
with at most 2 decimal places; all arithmetic and every API payload uses integer base
units, so a model can never introduce float rounding into a transfer.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal, InvalidOperation

DECIMALS = 6
_SCALE = Decimal(10) ** DECIMALS
_CENT = Decimal("0.01")


class AmountError(ValueError):
    pass


def to_base(display: float | int | str | Decimal) -> int:
    """Convert a display amount (max 2 decimals) to integer base units."""
    try:
        value = Decimal(str(display))
    except InvalidOperation as exc:
        raise AmountError(f"not a number: {display!r}") from exc
    if not value.is_finite():
        raise AmountError("amount must be finite")
    if value != value.quantize(_CENT):
        raise AmountError("amount may have at most 2 decimal places")
    return int(value * _SCALE)


def to_display(base: int) -> str:
    """Format base units for prompts and messages, truncating below a cent."""
    value = (Decimal(base) / _SCALE).quantize(_CENT, rounding=ROUND_DOWN)
    return f"{value:.2f}"
