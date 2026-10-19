"""Used proofs per guardian: a receipt that backed a transfer for one dependent can't back
another for a sibling.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("used_proof_keys", sa.Column("guardian_user_id", sa.UUID(), nullable=True))
    op.execute(
        "UPDATE used_proof_keys u SET guardian_user_id = f.guardian_user_id "
        "FROM families f WHERE f.id = u.family_id"
    )
    # Siblings who already used the same key: keep the earliest, it is what made it used.
    op.execute(
        "DELETE FROM used_proof_keys u USING used_proof_keys v "
        "WHERE u.guardian_user_id = v.guardian_user_id AND u.key = v.key "
        "AND (u.created_at, u.family_id) > (v.created_at, v.family_id)"
    )
    op.alter_column("used_proof_keys", "guardian_user_id", nullable=False)
    op.drop_constraint(op.f("pk_used_proof_keys"), "used_proof_keys", type_="primary")
    op.create_primary_key(
        op.f("pk_used_proof_keys"), "used_proof_keys", ["guardian_user_id", "key"]
    )
    op.create_foreign_key(
        op.f("fk_used_proof_keys_guardian_user_id_users"),
        "used_proof_keys",
        "users",
        ["guardian_user_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_used_proof_keys_guardian_user_id_users"), "used_proof_keys", type_="foreignkey"
    )
    op.drop_constraint(op.f("pk_used_proof_keys"), "used_proof_keys", type_="primary")
    op.create_primary_key(op.f("pk_used_proof_keys"), "used_proof_keys", ["family_id", "key"])
    op.drop_column("used_proof_keys", "guardian_user_id")
