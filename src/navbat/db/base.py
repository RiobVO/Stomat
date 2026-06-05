"""Подключение к БД и тенант-контекст.

Приложение ходит в БД под непривилегированной ролью navbat_app;
изоляция арендаторов — RLS по app.clinic_id (SET LOCAL на транзакцию).
"""
from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

# dev-DSN; в проде задаётся через env
DEFAULT_APP_DSN = "postgresql+psycopg://navbat_app:navbat_app_dev@localhost:5434/navbat"


def make_app_engine(dsn: str | None = None, **kwargs) -> Engine:
    return create_engine(dsn or os.environ.get("NAVBAT_APP_DSN", DEFAULT_APP_DSN), **kwargs)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def tenant_transaction(
    session_factory: sessionmaker[Session], clinic_id: uuid.UUID
) -> Iterator[Session]:
    """Транзакция с установленным тенант-контекстом.

    SET LOCAL живёт ровно одну транзакцию — RLS гарантированно не «протекает»
    в следующий чекаут соединения из пула.
    """
    with session_factory() as session:
        with session.begin():
            session.execute(
                text("SELECT set_config('app.clinic_id', :cid, true)"),
                {"cid": str(clinic_id)},
            )
            yield session
