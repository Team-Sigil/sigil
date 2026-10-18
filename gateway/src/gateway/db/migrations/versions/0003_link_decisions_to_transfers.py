"""Link each agent decision to the transfer its run produced.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("decisions", sa.Column("transfer_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_decisions_transfer_id_transfers"),
        "decisions",
        "transfers",
        ["transfer_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_decisions_transfer_id_transfers"), "decisions", type_="foreignkey")
    op.drop_column("decisions", "transfer_id")
