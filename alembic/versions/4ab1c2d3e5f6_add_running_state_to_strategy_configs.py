"""add is_running + run_mode + run_started_at to strategy_configs

Persists the "this strategy is currently running" state so the bot can
auto-rehydrate after a restart. Without these columns, restarting the bot
loses all running_strategy_instances in-memory state and users have to
manually POST /api/v1/strategies for each config.

Revision ID: 4ab1c2d3e5f6
Revises: 8c4d2e7f1a90
Create Date: 2026-09-19 18:36:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "4ab1c2d3e5f6"
down_revision = "8c4d2e7f1a90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "strategy_configs",
        sa.Column(
            "is_running",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "strategy_configs",
        sa.Column("run_mode", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "strategy_configs",
        sa.Column("run_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_strategy_configs_user_running",
        "strategy_configs",
        ["user_id", "is_running"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_strategy_configs_user_running", table_name="strategy_configs"
    )
    op.drop_column("strategy_configs", "run_started_at")
    op.drop_column("strategy_configs", "run_mode")
    op.drop_column("strategy_configs", "is_running")
