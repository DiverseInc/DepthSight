"""Add r_multiplier column to trades table

Revision ID: 71f2c3a9e5b4
Revises: 114355827cb0
Create Date: 2026-08-22 08:10:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "71f2c3a9e5b4"
down_revision: Union[str, Sequence[str], None] = "114355827cb0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add r_multiplier column to trades table.

    r_multiplier = pnl_usd / initial_risk_usd
    Positive = winner (e.g. 2.0 means 2× risk profit), negative = loser, 0 = breakeven.
    """
    op.add_column(
        "trades",
        sa.Column("r_multiplier", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    """Remove r_multiplier column from trades table."""
    op.drop_column("trades", "r_multiplier")
