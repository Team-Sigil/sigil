"""Proof normalisation (original kept encrypted, perceptual hash) and receipt fingerprints
shared across families.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Append-only for the app role: a receipt that backed a transfer stays used.
GRANTS = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sigil_app') THEN
        GRANT SELECT, INSERT ON receipt_fingerprints TO sigil_app;
    END IF;
END
$$;
"""


def upgrade() -> None:
    op.create_table(
        "receipt_fingerprints",
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=False),
        sa.Column("transfer_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["family_id"], ["families.id"], name=op.f("fk_receipt_fingerprints_family_id_families")
        ),
        sa.ForeignKeyConstraint(
            ["transfer_id"],
            ["transfers.id"],
            name=op.f("fk_receipt_fingerprints_transfer_id_transfers"),
        ),
        sa.PrimaryKeyConstraint("key_hash", name=op.f("pk_receipt_fingerprints")),
    )
    op.add_column("proofs", sa.Column("original", sa.LargeBinary(), nullable=True))
    op.add_column("proofs", sa.Column("original_sha256", sa.String(length=64), nullable=True))
    op.add_column("proofs", sa.Column("dhash", sa.BigInteger(), nullable=True))
    op.execute(GRANTS)


def downgrade() -> None:
    op.drop_column("proofs", "dhash")
    op.drop_column("proofs", "original_sha256")
    op.drop_column("proofs", "original")
    op.drop_table("receipt_fingerprints")
