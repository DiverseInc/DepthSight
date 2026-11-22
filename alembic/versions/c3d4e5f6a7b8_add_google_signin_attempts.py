"""Add the table that makes a Google sign-in attempt single-use

ONE Google redirect must produce exactly ONE session. The client sends the
one-time value it generated for the attempt (the OAuth `state`) together with
the credential, and the server CONSUMES that value exactly once: a second
attempt with the same value is refused instead of minting a second session.

The consumed values live in the DATABASE rather than in the process memory
because the API is served by gunicorn with FIVE workers (Dockerfile:32). An
in-process set would be per worker, so two workers would each accept the same
credential and mint two sessions. The UNIQUE constraint on `value` is what
makes "consume once" atomic across workers, processes and restarts.

The credential is consumed by its sha256 digest as well, which covers a
client that sends no one-time value at all (the PWA popup flow) and makes a
replayed credential refuse to mint a second session.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-11-04 10:05:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the consumed-values table."""
    op.create_table(
        "google_signin_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        # Either the one-time value the client generated (prefix "state:")
        # or the sha256 digest of the credential (prefix "cred:").
        sa.Column("value", sa.String(length=160), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_google_signin_attempts_id"),
        "google_signin_attempts",
        ["id"],
        unique=False,
    )
    # The UNIQUE constraint is the whole point of this table: it is what
    # makes "consume once" atomic.
    op.create_index(
        op.f("ix_google_signin_attempts_value"),
        "google_signin_attempts",
        ["value"],
        unique=True,
    )
    op.create_index(
        op.f("ix_google_signin_attempts_created_at"),
        "google_signin_attempts",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the consumed-values table."""
    op.drop_index(
        op.f("ix_google_signin_attempts_created_at"),
        table_name="google_signin_attempts",
    )
    op.drop_index(
        op.f("ix_google_signin_attempts_value"),
        table_name="google_signin_attempts",
    )
    op.drop_index(
        op.f("ix_google_signin_attempts_id"),
        table_name="google_signin_attempts",
    )
    op.drop_table("google_signin_attempts")
