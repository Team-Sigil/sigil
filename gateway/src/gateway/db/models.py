"""All ORM models. One source of truth for the schema; migrations mirror this file.

Money columns are BIGINT base units (6 decimals) named like the agent contract (`amount`,
`amount_requested`, `already_sent`, ...), so rows map onto its payloads without renaming.
"""

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Dialect,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from gateway.core.config import get_settings
from gateway.core.crypto import decrypt, encrypt
from gateway.enums import (
    Audience,
    GuardianDecision,
    InterviewRole,
    InterviewStatus,
    InterviewTurnKind,
    NoncePurpose,
    ObligationStatus,
    PolicyStatus,
    ProposalStatus,
    QuestionStatus,
    Role,
    ThreadRole,
    TransferStatus,
    TransferTrigger,
    Verdict,
)

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

PUBKEY_LEN = 44
HASH_LEN = 64
GENESIS_HASH = "0" * HASH_LEN


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class EncryptedBytes(TypeDecorator[bytes]):
    """Fernet-encrypted at rest (SPEC SEC-15); plain bytes in Python."""

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value: bytes | None, dialect: Dialect) -> bytes | None:
        if value is None:
            return None
        return encrypt(get_settings().data_encryption_key.get_secret_value(), value)

    def process_result_value(self, value: bytes | None, dialect: Dialect) -> bytes | None:
        if value is None:
            return None
        return decrypt(get_settings().data_encryption_key.get_secret_value(), value)


class EncryptedText(TypeDecorator[str]):
    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> bytes | None:
        if value is None:
            return None
        return encrypt(get_settings().data_encryption_key.get_secret_value(), value.encode())

    def process_result_value(self, value: bytes | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return decrypt(get_settings().data_encryption_key.get_secret_value(), value).decode()


def str_enum(enum: type[StrEnum], name: str) -> SAEnum:
    """Stored as VARCHAR + CHECK on the enum values."""
    return SAEnum(
        enum,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda members: [m.value for m in members],
    )


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def family_fk(index: bool = True) -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey("families.id"), index=index)


# --- identity and policy ----------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    wallet_address: Mapped[str] = mapped_column(String(PUBKEY_LEN), unique=True)
    role: Mapped[Role] = mapped_column(str_enum(Role, "role"))
    display_name: Mapped[str | None] = mapped_column(String(80))
    locale: Mapped[str | None] = mapped_column(String(35))  # BCP 47, e.g. "zh-TW"
    created_at: Mapped[datetime] = created_at()


class Family(Base):
    """One guardian and one of their dependents. A guardian may have several (one family per
    dependent, each with its own policy, budget and freeze); a dependent has exactly one
    guardian. The agent addresses them by user id."""

    __tablename__ = "families"

    id: Mapped[uuid.UUID] = uuid_pk()
    guardian_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    dependent_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), unique=True)
    dependent_token_account: Mapped[str] = mapped_column(String(PUBKEY_LEN))
    guardian_token_account: Mapped[str] = mapped_column(String(PUBKEY_LEN))
    registration_signature: Mapped[str] = mapped_column(String(128))
    registration_nonce: Mapped[str] = mapped_column(String(64))
    timezone: Mapped[str] = mapped_column(String(64), server_default="Asia/Bangkok")
    frozen: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        CheckConstraint("guardian_user_id <> dependent_user_id", name="distinct_members"),
    )


class Nonce(Base):
    __tablename__ = "nonces"

    nonce: Mapped[str] = mapped_column(String(64), primary_key=True)
    wallet: Mapped[str] = mapped_column(String(PUBKEY_LEN))
    purpose: Mapped[NoncePurpose] = mapped_column(str_enum(NoncePurpose, "nonce_purpose"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Policy(Base):
    """`body` is the canonical policy in base units; the guardian signs its hash."""

    __tablename__ = "policies"

    id: Mapped[uuid.UUID] = uuid_pk()
    family_id: Mapped[uuid.UUID] = family_fk(index=False)
    version: Mapped[int] = mapped_column(Integer)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    body_hash: Mapped[str] = mapped_column(String(HASH_LEN))
    test_scenarios: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    open_questions: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    guardian_signature: Mapped[str | None] = mapped_column(String(128))
    activation_nonce: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[PolicyStatus] = mapped_column(str_enum(PolicyStatus, "policy_status"))
    created_at: Mapped[datetime] = created_at()
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("family_id", "version", name="uq_policies_family_version"),
        # Exactly one active policy per family.
        Index(
            "uq_policies_one_active",
            "family_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        CheckConstraint("version > 0", name="version_positive"),
    )


class PolicyInterview(Base):
    """The guardian's policy setup chat with the agent (protocol sigil.policy-interview/1).
    The agent is stateless: the gateway stores the conversation and sends it every turn."""

    __tablename__ = "policy_interviews"

    id: Mapped[uuid.UUID] = uuid_pk()
    family_id: Mapped[uuid.UUID] = family_fk(index=False)
    status: Mapped[InterviewStatus] = mapped_column(
        str_enum(InterviewStatus, "interview_status"), server_default=InterviewStatus.OPEN.value
    )
    draft_policy_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("policies.id"))
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        # At most one open interview per family.
        Index(
            "uq_policy_interviews_one_open",
            "family_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
    )


class PolicyInterviewTurn(Base):
    """One turn, in order. Agent turns are stored exactly as the agent returned them, with
    their `kind`; the app can never submit one."""

    __tablename__ = "policy_interview_turns"

    id: Mapped[uuid.UUID] = uuid_pk()
    interview_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("policy_interviews.id"))
    position: Mapped[int] = mapped_column(Integer)
    role: Mapped[InterviewRole] = mapped_column(str_enum(InterviewRole, "interview_role"))
    text: Mapped[str] = mapped_column(EncryptedText)
    kind: Mapped[InterviewTurnKind | None] = mapped_column(
        str_enum(InterviewTurnKind, "interview_turn_kind")
    )
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        UniqueConstraint("interview_id", "position", name="uq_policy_interview_turns_position"),
        CheckConstraint("(role = 'agent') = (kind IS NOT NULL)", name="kind_only_on_agent_turns"),
    )


# --- requests, conversation and proofs --------------------------------------------------------


class Request(Base):
    """A dependent's money request (`DependentRequest` in the agent contract)."""

    __tablename__ = "requests"

    id: Mapped[uuid.UUID] = uuid_pk()
    family_id: Mapped[uuid.UUID] = family_fk()
    message: Mapped[str] = mapped_column(EncryptedText)
    amount_requested: Mapped[int | None] = mapped_column(BigInteger)
    emergency_claimed: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    already_sent: Mapped[int] = mapped_column(BigInteger, server_default="0")
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        CheckConstraint(
            "amount_requested IS NULL OR amount_requested > 0", name="amount_requested_positive"
        ),
        CheckConstraint("already_sent >= 0", name="already_sent_non_negative"),
    )


class Message(Base):
    """A request's conversation thread (`ThreadMessage`)."""

    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = uuid_pk()
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("requests.id"))
    role: Mapped[ThreadRole] = mapped_column(str_enum(ThreadRole, "thread_role"))
    text: Mapped[str] = mapped_column(EncryptedText)
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (Index("ix_messages_request_created", "request_id", "created_at"),)


class Proof(Base):
    """An uploaded receipt, quote or photo with the integrity fields the agent and engine use.
    `content` holds the image bytes, or UTF-8 text for `text/plain` proofs."""

    __tablename__ = "proofs"

    id: Mapped[uuid.UUID] = uuid_pk()
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("requests.id"), index=True)
    family_id: Mapped[uuid.UUID] = family_fk()
    kind: Mapped[str] = mapped_column(String(16))  # receipt | quote | photo
    media_type: Mapped[str] = mapped_column(String(32))
    size_bytes: Mapped[int] = mapped_column(Integer)
    content: Mapped[bytes] = mapped_column(EncryptedBytes)  # the normalised copy the agent sees
    sha256: Mapped[str] = mapped_column(String(HASH_LEN))
    # Images only: the bytes as uploaded (kept as evidence), their hash, and a perceptual hash
    # of the stored copy for near-duplicate detection.
    original: Mapped[bytes | None] = mapped_column(EncryptedBytes)
    original_sha256: Mapped[str | None] = mapped_column(String(HASH_LEN))
    dhash: Mapped[int | None] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(16))  # photo | file | ocr | typed
    derived_from: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("proofs.id"))
    flags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    # Set only for a verified in-app capture (source "photo").
    device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("capture_devices.id"))
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uploaded_at: Mapped[datetime] = created_at()

    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="size_non_negative"),
        Index("ix_proofs_family_sha256", "family_id", "sha256"),
    )


class CaptureDevice(Base):
    """A dependent's iPhone with an App Attest key (Secure Enclave, attested by Apple). Only
    its signed captures become `source: "photo"`."""

    __tablename__ = "capture_devices"

    id: Mapped[uuid.UUID] = uuid_pk()
    family_id: Mapped[uuid.UUID] = family_fk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    key_id: Mapped[str] = mapped_column(String(100), unique=True)  # base64 App Attest key id
    public_key: Mapped[bytes] = mapped_column(LargeBinary)  # SubjectPublicKeyInfo DER
    counter: Mapped[int] = mapped_column(BigInteger, server_default="0")  # against replays
    created_at: Mapped[datetime] = created_at()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CaptureTicket(Base):
    """A single-use, 5-minute nonce for one capture on one request with one device."""

    __tablename__ = "capture_tickets"

    nonce: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("requests.id"))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("capture_devices.id"))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReceiptFingerprint(Base):
    """A receipt that backed a transfer, across all families, as a keyed hash of merchant,
    date, total and receipt number: the same receipt cannot back transfers in two families,
    and the table reveals nothing about the receipts themselves."""

    __tablename__ = "receipt_fingerprints"

    key_hash: Mapped[str] = mapped_column(String(HASH_LEN), primary_key=True)
    family_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("families.id"))
    transfer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transfers.id"))
    created_at: Mapped[datetime] = created_at()


class UsedProofKey(Base):
    """A proof id, content hash or receipt fingerprint that already backed a transfer for any of
    the guardian's dependents (siblings can't reuse each other's receipts). Inserted in the same
    transaction as the transfer, so one receipt backs one transfer."""

    __tablename__ = "used_proof_keys"

    guardian_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    family_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("families.id"))
    transfer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transfers.id"))
    created_at: Mapped[datetime] = created_at()


# --- money ------------------------------------------------------------------------------------


class AuditorVerdict(Base):
    """Posted only with the Auditor's token, bound to one exact transfer, usable once."""

    __tablename__ = "auditor_verdicts"

    id: Mapped[uuid.UUID] = uuid_pk()
    family_id: Mapped[uuid.UUID] = family_fk()
    request_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("requests.id"))
    idempotency_key: Mapped[str] = mapped_column(String(64))
    amount: Mapped[int] = mapped_column(BigInteger)
    category: Mapped[str] = mapped_column(String(32))
    verdict: Mapped[Verdict] = mapped_column(str_enum(Verdict, "verdict"))
    reason: Mapped[str] = mapped_column(Text)
    concerns: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    independent_assessment: Mapped[dict[str, Any]] = mapped_column(JSONB)
    review_triggers: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    model: Mapped[str] = mapped_column(String(100))
    used: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (CheckConstraint("amount > 0", name="amount_positive"),)


class Transfer(Base):
    """One row per idempotency key: the engine's decision on a `TransferInstruction` and, when
    cleared, the transfer's progress. Its id is the `decision_id` returned to the agent."""

    __tablename__ = "transfers"

    id: Mapped[uuid.UUID] = uuid_pk()
    family_id: Mapped[uuid.UUID] = family_fk()
    request_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("requests.id"), index=True)
    trigger: Mapped[TransferTrigger] = mapped_column(str_enum(TransferTrigger, "trigger"))
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    purpose: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(32))
    category_confidence: Mapped[float] = mapped_column(Float)
    reasoning: Mapped[str] = mapped_column(Text)
    policy_version: Mapped[int] = mapped_column(Integer)
    auditor_verdict_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("auditor_verdicts.id"))
    proof_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    proof_findings: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[TransferStatus] = mapped_column(str_enum(TransferStatus, "transfer_status"))
    # Set when the transfer cleared at or above the proof threshold without proof: once it is
    # sent, the dependent owes a receipt (SPEC §8.9).
    proof_owed: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    reason: Mapped[str | None] = mapped_column(String(300))
    destination_token_account: Mapped[str | None] = mapped_column(String(PUBKEY_LEN))
    tx_signature: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_transfers_family_created", "family_id", "created_at"),
    )


class ProofObligation(Base):
    """A receipt the dependent owes for money already sent.

    Opened when a transfer at or above `proof_threshold` is sent without proof, and closed
    when a proof arrives that counts, covers the amount and has not backed a transfer before.
    While one is open the dependent cannot ask for more money (SPEC §8.9).
    """

    __tablename__ = "proof_obligations"

    id: Mapped[uuid.UUID] = uuid_pk()
    family_id: Mapped[uuid.UUID] = family_fk()
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("requests.id"), index=True)
    transfer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transfers.id"), unique=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[ObligationStatus] = mapped_column(
        str_enum(ObligationStatus, "obligation_status")
    )
    # The proof that settled it, and what the agent read from that proof.
    proof_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("proofs.id"))
    findings: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at()
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_proof_obligations_family_status", "family_id", "status"),)


class BudgetCounter(Base):
    """Per family and budget week (Monday 00:00 in the family timezone). Updated only under
    `SELECT ... FOR UPDATE` (SPEC SEC-08)."""

    __tablename__ = "budget_counters"

    family_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("families.id"), primary_key=True)
    week_start: Mapped[date] = mapped_column(Date, primary_key=True)
    spent: Mapped[int] = mapped_column(BigInteger, server_default="0")
    reserved: Mapped[int] = mapped_column(BigInteger, server_default="0")

    __table_args__ = (
        CheckConstraint("spent >= 0", name="spent_non_negative"),
        CheckConstraint("reserved >= 0", name="reserved_non_negative"),
    )


# --- guardian interaction ---------------------------------------------------------------------


class GuardianQuestion(Base):
    """Asked by the agent; answered only by a wallet-signed message (SPEC SEC-05)."""

    __tablename__ = "guardian_questions"

    id: Mapped[uuid.UUID] = uuid_pk()
    family_id: Mapped[uuid.UUID] = family_fk(index=False)
    request_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("requests.id"))
    question: Mapped[str] = mapped_column(Text)
    recommendation: Mapped[str] = mapped_column(String(16))
    recommended_amount: Mapped[int | None] = mapped_column(BigInteger)
    urgency: Mapped[str] = mapped_column(String(8))
    category: Mapped[str | None] = mapped_column(String(32))
    agent_view: Mapped[str] = mapped_column(Text)
    auditor_view: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    status: Mapped[QuestionStatus] = mapped_column(
        str_enum(QuestionStatus, "question_status"), server_default=QuestionStatus.OPEN.value
    )
    decision: Mapped[GuardianDecision | None] = mapped_column(
        str_enum(GuardianDecision, "guardian_decision")
    )
    approved_amount: Mapped[int | None] = mapped_column(BigInteger)
    note: Mapped[str | None] = mapped_column(String(300))
    signature: Mapped[str | None] = mapped_column(String(128))
    answer_nonce: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = created_at()
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "approved_amount IS NULL OR approved_amount > 0", name="approved_amount_positive"
        ),
        Index("ix_guardian_questions_family_status", "family_id", "status"),
    )


class PolicyProposal(Base):
    __tablename__ = "policy_proposals"

    id: Mapped[uuid.UUID] = uuid_pk()
    family_id: Mapped[uuid.UUID] = family_fk()
    change: Mapped[str] = mapped_column(Text)
    evidence: Mapped[str] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    status: Mapped[ProposalStatus] = mapped_column(
        str_enum(ProposalStatus, "proposal_status"), server_default=ProposalStatus.OPEN.value
    )
    created_at: Mapped[datetime] = created_at()


class ProofRequest(Base):
    __tablename__ = "proof_requests"

    id: Mapped[uuid.UUID] = uuid_pk()
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("requests.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = uuid_pk()
    # None for a guardian-wide note (the agent's notifications name only the recipient, and a
    # guardian may have several dependents, e.g. "top up the shared reservoir").
    family_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("families.id"), index=True)
    audience: Mapped[Audience] = mapped_column(str_enum(Audience, "audience"))
    recipient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    message: Mapped[str] = mapped_column(EncryptedText)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = created_at()
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --- agent records ----------------------------------------------------------------------------


class Decision(Base):
    """The agent's `DecisionRecord`: how a run ended and what it told each person."""

    __tablename__ = "decisions"

    id: Mapped[uuid.UUID] = uuid_pk()
    family_id: Mapped[uuid.UUID] = family_fk()
    request_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("requests.id"), index=True)
    outcome: Mapped[str] = mapped_column(String(32))
    reply_to_dependent: Mapped[str] = mapped_column(EncryptedText)
    summary_for_guardian: Mapped[str] = mapped_column(EncryptedText)
    amount_sent: Mapped[int] = mapped_column(BigInteger)
    policy_version: Mapped[int | None] = mapped_column(Integer)
    guardrail_events: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    # The transfer this run produced, linked by the gateway (agent layer gap 3).
    transfer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("transfers.id"))
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (CheckConstraint("amount_sent >= 0", name="amount_sent_non_negative"),)


class AgentEvent(Base):
    __tablename__ = "agent_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    kind: Mapped[str] = mapped_column(String(64))
    subject_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at()


class LlmUsage(Base):
    """Tokens per guardian per day: the agent's usage "family" is the guardian, across all of
    their dependents."""

    __tablename__ = "llm_usage"

    guardian_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    tokens_used: Mapped[int] = mapped_column(BigInteger, server_default="0")

    __table_args__ = (CheckConstraint("tokens_used >= 0", name="tokens_non_negative"),)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    family_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("families.id"))
    event_type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    prev_hash: Mapped[str] = mapped_column(String(HASH_LEN))
    hash: Mapped[str] = mapped_column(String(HASH_LEN), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_audit_log_family_id", "family_id", "id"),)
