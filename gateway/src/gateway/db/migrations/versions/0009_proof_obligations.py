"""Proof after payment: a transfer at or above the proof threshold goes out first and leaves
the dependent owing a receipt, which blocks further requests until it is settled.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "transfers",
        sa.Column("proof_owed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    obligation_status = sa.Enum("open", "settled", name="obligation_status")
    obligation_status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "proof_obligations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=False),
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("transfer_id", sa.UUID(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("status", obligation_status, nullable=False),
        sa.Column("proof_id", sa.UUID(), nullable=True),
        sa.Column("findings", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["family_id"], ["families.id"], name=op.f("fk_proof_obligations_family_id_families")),
        sa.ForeignKeyConstraint(["request_id"], ["requests.id"], name=op.f("fk_proof_obligations_request_id_requests")),
        sa.ForeignKeyConstraint(["transfer_id"], ["transfers.id"], name=op.f("fk_proof_obligations_transfer_id_transfers")),
        sa.ForeignKeyConstraint(["proof_id"], ["proofs.id"], name=op.f("fk_proof_obligations_proof_id_proofs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_proof_obligations")),
        sa.UniqueConstraint("transfer_id", name=op.f("uq_proof_obligations_transfer_id")),
    )
    op.create_index("ix_proof_obligations_family_status", "proof_obligations", ["family_id", "status"])
    op.create_index(op.f("ix_proof_obligations_family_id"), "proof_obligations", ["family_id"])
    op.create_index(op.f("ix_proof_obligations_request_id"), "proof_obligations", ["request_id"])


def downgrade() -> None:
    op.drop_table("proof_obligations")
    sa.Enum(name="obligation_status").drop(op.get_bind(), checkfirst=True)
    op.drop_column("transfers", "proof_owed")
