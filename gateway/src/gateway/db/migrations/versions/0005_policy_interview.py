"""Policy interview: the stored guardian-agent conversation (sigil.policy-interview/1).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Turns are append-only for the app role: a stored agent turn (and its kind) is never edited.
GRANTS = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sigil_app') THEN
        GRANT SELECT, INSERT, UPDATE ON policy_interviews TO sigil_app;
        GRANT SELECT, INSERT ON policy_interview_turns TO sigil_app;
    END IF;
END
$$;
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "policy_interviews",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "open",
                "completed",
                "abandoned",
                name="interview_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="open",
            nullable=False,
        ),
        sa.Column("draft_policy_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["draft_policy_id"],
            ["policies.id"],
            name=op.f("fk_policy_interviews_draft_policy_id_policies"),
        ),
        sa.ForeignKeyConstraint(
            ["family_id"], ["families.id"], name=op.f("fk_policy_interviews_family_id_families")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_interviews")),
    )
    op.create_index(
        "uq_policy_interviews_one_open",
        "policy_interviews",
        ["family_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_table(
        "policy_interview_turns",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("interview_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "guardian",
                "agent",
                name="interview_role",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("text", sa.LargeBinary(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "question",
                "confirmation",
                "review",
                name="interview_turn_kind",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(role = 'agent') = (kind IS NOT NULL)",
            name=op.f("ck_policy_interview_turns_kind_only_on_agent_turns"),
        ),
        sa.ForeignKeyConstraint(
            ["interview_id"],
            ["policy_interviews.id"],
            name=op.f("fk_policy_interview_turns_interview_id_policy_interviews"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_interview_turns")),
        sa.UniqueConstraint("interview_id", "position", name="uq_policy_interview_turns_position"),
    )

    op.execute(GRANTS)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("policy_interview_turns")
    op.drop_index(
        "uq_policy_interviews_one_open",
        table_name="policy_interviews",
        postgresql_where=sa.text("status = 'open'"),
    )
    op.drop_table("policy_interviews")
