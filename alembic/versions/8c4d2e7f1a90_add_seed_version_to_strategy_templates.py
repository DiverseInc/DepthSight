"""Add seed_version column to strategy_templates table

Revision ID: 8c4d2e7f1a90
Revises: 71f2c3a9e5b4
Create Date: 2026-08-22 11:13:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "8c4d2e7f1a90"
down_revision: Union[str, Sequence[str], None] = "71f2c3a9e5b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add seed_version column to strategy_templates.

    seed_version is an integer that the lifespan seeder compares against the
    SEED_VERSION constant in api/crud.py. If the constant is newer than the
    row's stored seed_version, the seeder re-applies the built-in template's
    config_data + risk_profile fields, preventing stale-DB bit-rot when the
    7 built-in templates change.

    Existing rows default to seed_version=0, which is "older than any defined
    seed" — so the next api restart will refresh every row to SEED_VERSION
    (currently 1). This is exactly what we want for the migration step.
    """
    op.add_column(
        "strategy_templates",
        sa.Column(
            "seed_version",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    """Remove seed_version column from strategy_templates."""
    op.drop_column("strategy_templates", "seed_version")
