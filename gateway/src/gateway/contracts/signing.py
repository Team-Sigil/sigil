"""Exact texts of guardian-signed messages (SPEC §7.5).

The iOS app must produce byte-identical text; the server always rebuilds the message from its own
data and the submitted nonce, never trusting a client-supplied message.
"""

from uuid import UUID

from gateway.enums import GuardianDecision

APP_TAG = "FinGuard"


def activate_policy_message(family_id: UUID, version: int, body_hash: str, nonce: str) -> str:
    return (
        f"{APP_TAG}:activate-policy\n"
        f"family:{family_id}\n"
        f"version:{version}\n"
        f"hash:{body_hash}\n"
        f"nonce:{nonce}"
    )


def answer_question_message(
    family_id: UUID,
    question_id: UUID,
    decision: GuardianDecision,
    amount_units: int | None,
    nonce: str,
) -> str:
    amount = "none" if amount_units is None else str(amount_units)
    return (
        f"{APP_TAG}:answer-question\n"
        f"family:{family_id}\n"
        f"question:{question_id}\n"
        f"decision:{decision.value}\n"
        f"amount_units:{amount}\n"
        f"nonce:{nonce}"
    )


def freeze_message(family_id: UUID, frozen: bool, nonce: str) -> str:
    return (
        f"{APP_TAG}:freeze\n"
        f"family:{family_id}\n"
        f"frozen:{'true' if frozen else 'false'}\n"
        f"nonce:{nonce}"
    )


def register_dependent_message(
    family_id: UUID | None,
    dependent_wallet: str,
    dependent_token_account: str,
    nonce: str,
) -> str:
    family = "new" if family_id is None else str(family_id)
    return (
        f"{APP_TAG}:register-dependent\n"
        f"family:{family}\n"
        f"dependent_wallet:{dependent_wallet}\n"
        f"dependent_token_account:{dependent_token_account}\n"
        f"nonce:{nonce}"
    )
