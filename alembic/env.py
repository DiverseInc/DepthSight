# alembic/env.py

import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))


from logging.config import fileConfig


from alembic import context


from api.models import (
    Base,
)
from api.database import (
    alembic_database_url,
)


# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config


# The URL Alembic connects with comes from the environment, via
# api.database.alembic_database_url().
#
# It MUST name the sync driver (psycopg2) explicitly. The previous code did
# DATABASE_URL.replace("+asyncpg", ""), which leaves the BARE "postgresql://"
# scheme; on SQLAlchemy 2.x that does not mean "no driver", it means "pick
# one", and it picks psycopg3 -- which is not installed.  `alembic upgrade
# head` therefore died with "ModuleNotFoundError: No module named 'psycopg'",
# and docker-startup.sh swallowed that, so the API served traffic while the
# schema never advanced.
config.set_main_option("sqlalchemy.url", alembic_database_url())


if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    from sqlalchemy import create_engine

    url = config.get_main_option("sqlalchemy.url")
    connectable = create_engine(url)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
