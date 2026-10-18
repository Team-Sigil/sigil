"""Verified in-app capture: attested devices, capture tickets, proof capture fields.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PURPOSES_BEFORE = ("login", "activate-policy", "answer-question", "freeze", "register-dependent")
PURPOSES_AFTER = (*PURPOSES_BEFORE, "enrol-device")

GRANTS = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sigil_app') THEN
        GRANT SELECT, INSERT, UPDATE ON capture_devices, capture_tickets TO sigil_app;
    END IF;
END
$$;
"""


def _purpose_check(purposes: tuple[str, ...]) -> None:
    op.drop_constraint(op.f("ck_nonces_nonce_purpose"), "nonces", type_="check")
    values = ", ".join(f"'{p}'" for p in purposes)
    op.create_check_constraint(op.f("ck_nonces_nonce_purpose"), "nonces", f"purpose IN ({values})")


def upgrade() -> None:
    op.create_table(
        "capture_devices",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column(
            "platform",
            sa.Enum(
                "ios",
                "android",
                name="device_platform",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("key_id", sa.String(length=100), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("counter", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("security_level", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["family_id"], ["families.id"], name=op.f("fk_capture_devices_family_id_families")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_capture_devices_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_capture_devices")),
        sa.UniqueConstraint("key_id", name=op.f("uq_capture_devices_key_id")),
    )
    op.create_index(
        op.f("ix_capture_devices_family_id"), "capture_devices", ["family_id"], unique=False
    )
    op.create_table(
        "capture_tickets",
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("device_id", sa.UUID(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["capture_devices.id"],
            name=op.f("fk_capture_tickets_device_id_capture_devices"),
        ),
        sa.ForeignKeyConstraint(
            ["request_id"], ["requests.id"], name=op.f("fk_capture_tickets_request_id_requests")
        ),
        sa.PrimaryKeyConstraint("nonce", name=op.f("pk_capture_tickets")),
    )
    op.add_column("proofs", sa.Column("device_id", sa.UUID(), nullable=True))
    op.add_column("proofs", sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        op.f("fk_proofs_device_id_capture_devices"),
        "proofs",
        "capture_devices",
        ["device_id"],
        ["id"],
    )
    _purpose_check(PURPOSES_AFTER)
    op.execute(GRANTS)


def downgrade() -> None:
    op.execute("DELETE FROM nonces WHERE purpose = 'enrol-device'")
    _purpose_check(PURPOSES_BEFORE)
    op.drop_constraint(op.f("fk_proofs_device_id_capture_devices"), "proofs", type_="foreignkey")
    op.drop_column("proofs", "captured_at")
    op.drop_column("proofs", "device_id")
    op.drop_table("capture_tickets")
    op.drop_index(op.f("ix_capture_devices_family_id"), table_name="capture_devices")
    op.drop_table("capture_devices")
