"""The signer's view also names each family's source token account (the guardian's reservoir),
so the signer can check where money comes from as well as where it goes.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-19
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GRANT = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sigil_signer_ro') THEN
        GRANT SELECT ON registered_destinations TO sigil_signer_ro;
    END IF;
END
$$;
"""


def upgrade() -> None:
    # OR REPLACE may add columns at the end; the signer role keeps its grant.
    op.execute(
        "CREATE OR REPLACE VIEW registered_destinations AS "
        "SELECT id AS family_id, dependent_token_account, guardian_token_account FROM families"
    )


def downgrade() -> None:
    op.execute("DROP VIEW registered_destinations")
    op.execute(
        "CREATE VIEW registered_destinations AS "
        "SELECT id AS family_id, dependent_token_account FROM families"
    )
    op.execute(GRANT)
