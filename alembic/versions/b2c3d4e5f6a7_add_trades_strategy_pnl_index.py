"""Add the index the per-strategy realized PnL aggregate needs

The state publisher now reads the realized PnL of every running strategy
with ONE aggregate query over the `trades` table, grouped by
`(strategy_config_id, trade_mode)`.

The table only had SINGLE-column indexes on `strategy_config_id` and on
`trade_mode` (and none on `user_id` at all), so the aggregate had to pick
one of them and then filter the rest off the heap. This adds the
composite index the query is actually shaped for:

    user_id = ? AND strategy_config_id IN (...) AND trade_mode IN (...)

with `is_final_exit` as the last key so the finalized rows are found in
the index itself.

Revision ID: b2c3d4e5f6a7
Revises: 4ab1c2d3e5f6
Create Date: 2026-10-25 19:10:00.000000

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "4ab1c2d3e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


INDEX_NAME = "ix_trades_user_config_mode_final"


def upgrade() -> None:
    """Create the composite index for the per-strategy PnL aggregate.

    `CREATE INDEX CONCURRENTLY` is not used: this migration runs inside the
    normal alembic transaction, and a non-concurrent build on `trades` is
    short. If this table ever grows large enough for that to matter, the
    index should be built concurrently in a separate, autocommit migration.
    """
    op.create_index(
        INDEX_NAME,
        "trades",
        ["user_id", "strategy_config_id", "trade_mode", "is_final_exit"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the composite index."""
    op.drop_index(INDEX_NAME, table_name="trades")
