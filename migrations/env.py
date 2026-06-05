"""Alembic env: миграции гоняются под админ-ролью (владелец схемы)."""
import os

from alembic import context
from sqlalchemy import create_engine

config = context.config

# env переопределяет dev-DSN из alembic.ini
url = os.environ.get("NAVBAT_ADMIN_DSN", config.get_main_option("sqlalchemy.url"))


def run_migrations_offline() -> None:
    context.configure(url=url, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
