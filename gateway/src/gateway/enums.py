"""Finite states stored by the gateway. Values that cross the agent boundary match the agent
layer's `contracts.py` exactly."""

from enum import StrEnum


class Role(StrEnum):
    GUARDIAN = "guardian"
    DEPENDENT = "dependent"


class NoncePurpose(StrEnum):
    LOGIN = "login"
    ACTIVATE_POLICY = "activate-policy"
    ANSWER_QUESTION = "answer-question"
    FREEZE = "freeze"
    REGISTER_DEPENDENT = "register-dependent"
    ENROL_DEVICE = "enrol-device"  # App Attest challenge for a capture device


class PolicyStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class ThreadRole(StrEnum):
    """`ThreadMessage.role` in the agent contract."""

    DEPENDENT = "dependent"
    AGENT = "agent"
    GUARDIAN_ANSWER = "guardian_answer"
    SYSTEM_EVENT = "system_event"


class TransferTrigger(StrEnum):
    REQUEST = "request"
    ALLOWANCE = "allowance"


class TransferStatus(StrEnum):
    """`blocked`: refused by the engine. `pending`: cleared and reserved, sent to the crypto
    layer. `sent`: confirmed on chain. `failed`: the crypto layer refused or failed."""

    BLOCKED = "blocked"
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class Verdict(StrEnum):
    AGREE = "agree"
    FLAG = "flag"


class QuestionStatus(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"


class GuardianDecision(StrEnum):
    APPROVE = "approve"
    DENY = "deny"


class ProposalStatus(StrEnum):
    OPEN = "open"
    DISMISSED = "dismissed"
    ADOPTED = "adopted"


class Audience(StrEnum):
    GUARDIAN = "guardian"
    DEPENDENT = "dependent"


class AuditEvent(StrEnum):
    """Events written to the hash-chained audit log."""

    LOGIN = "login"
    FAMILY_REGISTERED = "family_registered"
    POLICY_ACTIVATED = "policy_activated"
    FREEZE_CHANGED = "freeze_changed"
    REQUEST_CREATED = "request_created"
    PROOF_UPLOADED = "proof_uploaded"
    VERDICT_RECORDED = "verdict_recorded"
    TRANSFER_BLOCKED = "transfer_blocked"
    TRANSFER_PENDING = "transfer_pending"
    TRANSFER_SENT = "transfer_sent"
    TRANSFER_FAILED = "transfer_failed"
    QUESTION_ASKED = "question_asked"
    QUESTION_ANSWERED = "question_answered"
    DECISION_RECORDED = "decision_recorded"
    DEVICE_ENROLLED = "device_enrolled"
    DEVICE_REVOKED = "device_revoked"
