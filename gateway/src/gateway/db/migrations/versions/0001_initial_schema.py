"""Initial schema, DB grants, signer view and append-only audit log.

Revision ID: 0001
Revises:
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Defense in depth for SEC-16: even a role with UPDATE/DELETE cannot rewrite history.
AUDIT_LOG_IMMUTABLE = """
CREATE FUNCTION audit_log_reject_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END
$$;
CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_reject_change();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_reject_change();
"""

# A read-only role sees only (family_id, dependent_token_account), for the signer's own
# destination check (SPEC §10). Whether the crypto layer reads this or asks our API is open.
REGISTERED_DESTINATIONS_VIEW = """
CREATE VIEW registered_destinations AS
    SELECT id AS family_id, dependent_token_account FROM families;
"""

# Least privilege for the app role: append-only tables get INSERT/SELECT only. Roles are
# created by infra/postgres/init-roles.sh; grants are skipped where a role does not exist.
GRANTS = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sigil_app') THEN
        GRANT SELECT, INSERT, UPDATE ON users, families, policies, requests, auditor_verdicts,
            transfers, budget_counters, guardian_questions, policy_proposals, notifications,
            llm_usage TO sigil_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON nonces TO sigil_app;
        GRANT SELECT, INSERT ON messages, proofs, used_proof_keys, proof_requests, decisions,
            agent_events, audit_log TO sigil_app;
        GRANT USAGE ON SEQUENCE audit_log_id_seq TO sigil_app;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sigil_signer_ro') THEN
        GRANT SELECT ON registered_destinations TO sigil_signer_ro;
    END IF;
END
$$;
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "agent_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_events")),
    )
    op.create_table(
        "nonces",
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column("wallet", sa.String(length=44), nullable=False),
        sa.Column(
            "purpose",
            sa.Enum(
                "login",
                "activate-policy",
                "answer-question",
                "freeze",
                "register-dependent",
                name="nonce_purpose",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("nonce", name=op.f("pk_nonces")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("wallet_address", sa.String(length=44), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "guardian",
                "dependent",
                name="role",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("display_name", sa.String(length=80), nullable=True),
        sa.Column("locale", sa.String(length=35), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("wallet_address", name=op.f("uq_users_wallet_address")),
    )
    op.create_table(
        "families",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("guardian_user_id", sa.UUID(), nullable=False),
        sa.Column("dependent_user_id", sa.UUID(), nullable=False),
        sa.Column("dependent_token_account", sa.String(length=44), nullable=False),
        sa.Column("guardian_token_account", sa.String(length=44), nullable=False),
        sa.Column("registration_signature", sa.String(length=128), nullable=False),
        sa.Column("registration_nonce", sa.String(length=64), nullable=False),
        sa.Column("timezone", sa.String(length=64), server_default="Asia/Bangkok", nullable=False),
        sa.Column("frozen", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "guardian_user_id <> dependent_user_id", name=op.f("ck_families_distinct_members")
        ),
        sa.ForeignKeyConstraint(
            ["dependent_user_id"], ["users.id"], name=op.f("fk_families_dependent_user_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["guardian_user_id"], ["users.id"], name=op.f("fk_families_guardian_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_families")),
        sa.UniqueConstraint("dependent_user_id", name=op.f("uq_families_dependent_user_id")),
        sa.UniqueConstraint("guardian_user_id", name=op.f("uq_families_guardian_user_id")),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=True),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("prev_hash", sa.String(length=64), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["family_id"], ["families.id"], name=op.f("fk_audit_log_family_id_families")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
        sa.UniqueConstraint("hash", name=op.f("uq_audit_log_hash")),
    )
    op.create_index("ix_audit_log_family_id", "audit_log", ["family_id", "id"], unique=False)
    op.create_table(
        "budget_counters",
        sa.Column("family_id", sa.UUID(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("spent", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("reserved", sa.BigInteger(), server_default="0", nullable=False),
        sa.CheckConstraint("reserved >= 0", name=op.f("ck_budget_counters_reserved_non_negative")),
        sa.CheckConstraint("spent >= 0", name=op.f("ck_budget_counters_spent_non_negative")),
        sa.ForeignKeyConstraint(
            ["family_id"], ["families.id"], name=op.f("fk_budget_counters_family_id_families")
        ),
        sa.PrimaryKeyConstraint("family_id", "week_start", name=op.f("pk_budget_counters")),
    )
    op.create_table(
        "llm_usage",
        sa.Column("family_id", sa.UUID(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("tokens_used", sa.BigInteger(), server_default="0", nullable=False),
        sa.CheckConstraint("tokens_used >= 0", name=op.f("ck_llm_usage_tokens_non_negative")),
        sa.ForeignKeyConstraint(
            ["family_id"], ["families.id"], name=op.f("fk_llm_usage_family_id_families")
        ),
        sa.PrimaryKeyConstraint("family_id", "day", name=op.f("pk_llm_usage")),
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=False),
        sa.Column(
            "audience",
            sa.Enum(
                "guardian",
                "dependent",
                name="audience",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("recipient_id", sa.UUID(), nullable=False),
        sa.Column("message", sa.LargeBinary(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["family_id"], ["families.id"], name=op.f("fk_notifications_family_id_families")
        ),
        sa.ForeignKeyConstraint(
            ["recipient_id"], ["users.id"], name=op.f("fk_notifications_recipient_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_notifications_dedupe_key")),
    )
    op.create_index(
        op.f("ix_notifications_family_id"), "notifications", ["family_id"], unique=False
    )
    op.create_table(
        "policies",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("body_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "test_scenarios",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "open_questions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("guardian_signature", sa.String(length=128), nullable=True),
        sa.Column("activation_nonce", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "active",
                "superseded",
                name="policy_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("version > 0", name=op.f("ck_policies_version_positive")),
        sa.ForeignKeyConstraint(
            ["family_id"], ["families.id"], name=op.f("fk_policies_family_id_families")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policies")),
        sa.UniqueConstraint("family_id", "version", name="uq_policies_family_version"),
    )
    op.create_index(
        "uq_policies_one_active",
        "policies",
        ["family_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "policy_proposals",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=False),
        sa.Column("change", sa.Text(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "open",
                "dismissed",
                "adopted",
                name="proposal_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="open",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["family_id"], ["families.id"], name=op.f("fk_policy_proposals_family_id_families")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_proposals")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_policy_proposals_dedupe_key")),
    )
    op.create_index(
        op.f("ix_policy_proposals_family_id"), "policy_proposals", ["family_id"], unique=False
    )
    op.create_table(
        "requests",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=False),
        sa.Column("message", sa.LargeBinary(), nullable=False),
        sa.Column("amount_requested", sa.BigInteger(), nullable=True),
        sa.Column(
            "emergency_claimed", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("already_sent", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("already_sent >= 0", name=op.f("ck_requests_already_sent_non_negative")),
        sa.CheckConstraint(
            "amount_requested IS NULL OR amount_requested > 0",
            name=op.f("ck_requests_amount_requested_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["family_id"], ["families.id"], name=op.f("fk_requests_family_id_families")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_requests")),
    )
    op.create_index(op.f("ix_requests_family_id"), "requests", ["family_id"], unique=False)
    op.create_table(
        "auditor_verdicts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=False),
        sa.Column("request_id", sa.UUID(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column(
            "verdict",
            sa.Enum(
                "agree",
                "flag",
                name="verdict",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("concerns", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column(
            "independent_assessment", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "review_triggers", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("used", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_auditor_verdicts_amount_positive")),
        sa.ForeignKeyConstraint(
            ["family_id"], ["families.id"], name=op.f("fk_auditor_verdicts_family_id_families")
        ),
        sa.ForeignKeyConstraint(
            ["request_id"], ["requests.id"], name=op.f("fk_auditor_verdicts_request_id_requests")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auditor_verdicts")),
    )
    op.create_index(
        op.f("ix_auditor_verdicts_family_id"), "auditor_verdicts", ["family_id"], unique=False
    )
    op.create_table(
        "decisions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=False),
        sa.Column("request_id", sa.UUID(), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("reply_to_dependent", sa.LargeBinary(), nullable=False),
        sa.Column("summary_for_guardian", sa.LargeBinary(), nullable=False),
        sa.Column("amount_sent", sa.BigInteger(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=True),
        sa.Column(
            "guardrail_events", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount_sent >= 0", name=op.f("ck_decisions_amount_sent_non_negative")),
        sa.ForeignKeyConstraint(
            ["family_id"], ["families.id"], name=op.f("fk_decisions_family_id_families")
        ),
        sa.ForeignKeyConstraint(
            ["request_id"], ["requests.id"], name=op.f("fk_decisions_request_id_requests")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_decisions")),
    )
    op.create_index(op.f("ix_decisions_family_id"), "decisions", ["family_id"], unique=False)
    op.create_index(op.f("ix_decisions_request_id"), "decisions", ["request_id"], unique=False)
    op.create_table(
        "guardian_questions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=False),
        sa.Column("request_id", sa.UUID(), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("recommendation", sa.String(length=16), nullable=False),
        sa.Column("recommended_amount", sa.BigInteger(), nullable=True),
        sa.Column("urgency", sa.String(length=8), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=True),
        sa.Column("agent_view", sa.Text(), nullable=False),
        sa.Column("auditor_view", sa.Text(), nullable=True),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "open",
                "answered",
                name="question_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="open",
            nullable=False,
        ),
        sa.Column(
            "decision",
            sa.Enum(
                "approve",
                "deny",
                name="guardian_decision",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column("approved_amount", sa.BigInteger(), nullable=True),
        sa.Column("note", sa.String(length=300), nullable=True),
        sa.Column("signature", sa.String(length=128), nullable=True),
        sa.Column("answer_nonce", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "approved_amount IS NULL OR approved_amount > 0",
            name=op.f("ck_guardian_questions_approved_amount_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["family_id"], ["families.id"], name=op.f("fk_guardian_questions_family_id_families")
        ),
        sa.ForeignKeyConstraint(
            ["request_id"], ["requests.id"], name=op.f("fk_guardian_questions_request_id_requests")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_guardian_questions")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_guardian_questions_dedupe_key")),
    )
    op.create_index(
        "ix_guardian_questions_family_status",
        "guardian_questions",
        ["family_id", "status"],
        unique=False,
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "dependent",
                "agent",
                "guardian_answer",
                "system_event",
                name="thread_role",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("text", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["request_id"], ["requests.id"], name=op.f("fk_messages_request_id_requests")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
    )
    op.create_index(
        "ix_messages_request_created", "messages", ["request_id", "created_at"], unique=False
    )
    op.create_table(
        "proof_requests",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["request_id"], ["requests.id"], name=op.f("fk_proof_requests_request_id_requests")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_proof_requests")),
    )
    op.create_index(
        op.f("ix_proof_requests_request_id"), "proof_requests", ["request_id"], unique=False
    )
    op.create_table(
        "proofs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("media_type", sa.String(length=32), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("derived_from", sa.UUID(), nullable=True),
        sa.Column("flags", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("size_bytes >= 0", name=op.f("ck_proofs_size_non_negative")),
        sa.ForeignKeyConstraint(
            ["derived_from"], ["proofs.id"], name=op.f("fk_proofs_derived_from_proofs")
        ),
        sa.ForeignKeyConstraint(
            ["family_id"], ["families.id"], name=op.f("fk_proofs_family_id_families")
        ),
        sa.ForeignKeyConstraint(
            ["request_id"], ["requests.id"], name=op.f("fk_proofs_request_id_requests")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_proofs")),
    )
    op.create_index(op.f("ix_proofs_family_id"), "proofs", ["family_id"], unique=False)
    op.create_index("ix_proofs_family_sha256", "proofs", ["family_id", "sha256"], unique=False)
    op.create_index(op.f("ix_proofs_request_id"), "proofs", ["request_id"], unique=False)
    op.create_table(
        "transfers",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=False),
        sa.Column("request_id", sa.UUID(), nullable=True),
        sa.Column(
            "trigger",
            sa.Enum(
                "request",
                "allowance",
                name="trigger",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("purpose", sa.String(length=200), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("category_confidence", sa.Float(), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("auditor_verdict_id", sa.UUID(), nullable=True),
        sa.Column("proof_ids", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("proof_findings", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "blocked",
                "pending",
                "sent",
                "failed",
                name="transfer_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("reason", sa.String(length=300), nullable=True),
        sa.Column("destination_token_account", sa.String(length=44), nullable=True),
        sa.Column("tx_signature", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_transfers_amount_positive")),
        sa.ForeignKeyConstraint(
            ["auditor_verdict_id"],
            ["auditor_verdicts.id"],
            name=op.f("fk_transfers_auditor_verdict_id_auditor_verdicts"),
        ),
        sa.ForeignKeyConstraint(
            ["family_id"], ["families.id"], name=op.f("fk_transfers_family_id_families")
        ),
        sa.ForeignKeyConstraint(
            ["request_id"], ["requests.id"], name=op.f("fk_transfers_request_id_requests")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transfers")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_transfers_idempotency_key")),
    )
    op.create_index(
        "ix_transfers_family_created", "transfers", ["family_id", "created_at"], unique=False
    )
    op.create_index(op.f("ix_transfers_family_id"), "transfers", ["family_id"], unique=False)
    op.create_index(op.f("ix_transfers_request_id"), "transfers", ["request_id"], unique=False)
    op.create_table(
        "used_proof_keys",
        sa.Column("family_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.String(length=300), nullable=False),
        sa.Column("transfer_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["family_id"], ["families.id"], name=op.f("fk_used_proof_keys_family_id_families")
        ),
        sa.ForeignKeyConstraint(
            ["transfer_id"], ["transfers.id"], name=op.f("fk_used_proof_keys_transfer_id_transfers")
        ),
        sa.PrimaryKeyConstraint("family_id", "key", name=op.f("pk_used_proof_keys")),
    )

    op.execute(AUDIT_LOG_IMMUTABLE)
    op.execute(REGISTERED_DESTINATIONS_VIEW)
    op.execute(GRANTS)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP VIEW IF EXISTS registered_destinations")
    op.execute("DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log")
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_reject_change()")
    op.drop_table("used_proof_keys")
    op.drop_index(op.f("ix_transfers_request_id"), table_name="transfers")
    op.drop_index(op.f("ix_transfers_family_id"), table_name="transfers")
    op.drop_index("ix_transfers_family_created", table_name="transfers")
    op.drop_table("transfers")
    op.drop_index(op.f("ix_proofs_request_id"), table_name="proofs")
    op.drop_index("ix_proofs_family_sha256", table_name="proofs")
    op.drop_index(op.f("ix_proofs_family_id"), table_name="proofs")
    op.drop_table("proofs")
    op.drop_index(op.f("ix_proof_requests_request_id"), table_name="proof_requests")
    op.drop_table("proof_requests")
    op.drop_index("ix_messages_request_created", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_guardian_questions_family_status", table_name="guardian_questions")
    op.drop_table("guardian_questions")
    op.drop_index(op.f("ix_decisions_request_id"), table_name="decisions")
    op.drop_index(op.f("ix_decisions_family_id"), table_name="decisions")
    op.drop_table("decisions")
    op.drop_index(op.f("ix_auditor_verdicts_family_id"), table_name="auditor_verdicts")
    op.drop_table("auditor_verdicts")
    op.drop_index(op.f("ix_requests_family_id"), table_name="requests")
    op.drop_table("requests")
    op.drop_index(op.f("ix_policy_proposals_family_id"), table_name="policy_proposals")
    op.drop_table("policy_proposals")
    op.drop_index(
        "uq_policies_one_active",
        table_name="policies",
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_table("policies")
    op.drop_index(op.f("ix_notifications_family_id"), table_name="notifications")
    op.drop_table("notifications")
    op.drop_table("llm_usage")
    op.drop_table("budget_counters")
    op.drop_index("ix_audit_log_family_id", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("families")
    op.drop_table("users")
    op.drop_table("nonces")
    op.drop_table("agent_events")
