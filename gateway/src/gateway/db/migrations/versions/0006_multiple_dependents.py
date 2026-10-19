"""Multiple dependents per guardian: one family per dependent, usage counted per guardian,
guardian-wide notifications.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GRANTS = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sigil_app') THEN
        GRANT SELECT, INSERT, UPDATE ON llm_usage TO sigil_app;
    END IF;
END
$$;
"""


def _usage_table(owner_column: str, owner_table: str) -> None:
    """llm_usage only holds today's token counters, so it is recreated rather than migrated."""
    op.drop_table("llm_usage")
    op.create_table(
        "llm_usage",
        sa.Column(owner_column, sa.UUID(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("tokens_used", sa.BigInteger(), server_default="0", nullable=False),
        sa.CheckConstraint("tokens_used >= 0", name=op.f("ck_llm_usage_tokens_non_negative")),
        sa.ForeignKeyConstraint(
            [owner_column],
            [f"{owner_table}.id"],
            name=op.f(f"fk_llm_usage_{owner_column}_{owner_table}"),
        ),
        sa.PrimaryKeyConstraint(owner_column, "day", name=op.f("pk_llm_usage")),
    )
    op.execute(GRANTS)


def upgrade() -> None:
    # A guardian may now have several families (one per dependent); a dependent still has
    # exactly one guardian (families.dependent_user_id stays unique).
    op.drop_constraint(op.f("uq_families_guardian_user_id"), "families", type_="unique")
    op.create_index(
        op.f("ix_families_guardian_user_id"), "families", ["guardian_user_id"], unique=False
    )
    op.alter_column("notifications", "family_id", existing_type=sa.UUID(), nullable=True)
    _usage_table("guardian_user_id", "users")


def downgrade() -> None:
    _usage_table("family_id", "families")
    op.execute("DELETE FROM notifications WHERE family_id IS NULL")
    op.alter_column("notifications", "family_id", existing_type=sa.UUID(), nullable=False)
    op.drop_index(op.f("ix_families_guardian_user_id"), table_name="families")
    op.create_unique_constraint(
        op.f("uq_families_guardian_user_id"), "families", ["guardian_user_id"]
    )
