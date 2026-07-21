"""Фикстуры: реальный PostgreSQL из docker-compose, миграции, тенант-данные.

Админ-движок (superuser postgres) — только для сетапа и прямых SQL-проверок,
он обходит RLS. Все тесты поведения движка ходят под navbat_app.
"""
from __future__ import annotations

import base64
import json
import os
import time
import uuid
from datetime import date, datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from navbat.db.base import make_app_engine, make_session_factory

ADMIN_DSN = os.environ.get(
    "NAVBAT_ADMIN_DSN", "postgresql+psycopg://postgres:navbat_dev@localhost:5434/navbat"
)
TASHKENT = ZoneInfo("Asia/Tashkent")

# фиксированный тестовый ключ AES-256 (не секрет — только для тестов)
os.environ.setdefault("NAVBAT_ENC_KEY", base64.b64encode(b"test-key-32-bytes-padded-000000!").decode())

# График: пн–сб, две смены с обедом 13:00–14:00
WORKING_INTERVALS = {
    day: [["09:00", "13:00"], ["14:00", "18:00"]]
    for day in ("mon", "tue", "wed", "thu", "fri", "sat")
}


def next_monday() -> date:
    today = date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def next_sunday() -> date:
    return next_monday() + timedelta(days=6)


def at_tashkent(day: date, hhmm: str) -> datetime:
    """«09:00 в Ташкенте такого-то дня» → aware UTC datetime."""
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(day, dt_time(h, m), TASHKENT).astimezone(timezone.utc)


@pytest.fixture(scope="session")
def admin_engine():
    engine = create_engine(ADMIN_DSN)
    deadline = time.monotonic() + 30
    last_err = None
    while time.monotonic() < deadline:
        try:
            with engine.connect():
                break
        except Exception as e:  # postgres ещё поднимается
            last_err = e
            time.sleep(1)
    else:
        pytest.exit(f"PostgreSQL недоступен ({ADMIN_DSN}): {last_err}\n"
                    f"Подними его: docker compose up -d", returncode=2)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def migrated(admin_engine):
    from alembic import command
    from alembic.config import Config

    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location", os.path.join(os.path.dirname(__file__), "..", "migrations")
    )
    os.environ.setdefault("NAVBAT_ADMIN_DSN", ADMIN_DSN)
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
def app_session_factory(migrated):
    engine = make_app_engine()
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_tables(admin_engine, migrated):
    with admin_engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE appointment_audit, appointment, conversation, holiday, "
            "patient, doctor, service, clinic CASCADE"
        ))
    yield


# ── Тенант-данные (через админа: сетап, не поведение) ───────────────────────

@pytest.fixture
def clinic_a(admin_engine) -> uuid.UUID:
    return _make_clinic(admin_engine, "Clinic A")


@pytest.fixture
def clinic_b(admin_engine) -> uuid.UUID:
    return _make_clinic(admin_engine, "Clinic B")


def _make_clinic(admin_engine, name: str) -> uuid.UUID:
    cid = uuid.uuid4()
    with admin_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO clinic (id, name, salt, timezone) "
                 "VALUES (:id, :name, 'test-salt', 'Asia/Tashkent')"),
            {"id": cid, "name": name},
        )
    return cid


@pytest.fixture
def doctor_a(admin_engine, clinic_a) -> uuid.UUID:
    return make_doctor(admin_engine, clinic_a)


def make_doctor(admin_engine, clinic_id, buffer_min: int = 10,
                intervals: dict | None = None, name: str | None = None) -> uuid.UUID:
    from navbat.crypto import encrypt_text

    did = uuid.uuid4()
    with admin_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO doctor (id, clinic_id, name_encrypted, working_intervals, "
                 "buffer_min) VALUES (:id, :cid, :name, :wi, :buf)"),
            {"id": did, "cid": clinic_id,
             "name": encrypt_text(name) if name else None,
             "wi": json.dumps(intervals or WORKING_INTERVALS), "buf": buffer_min},
        )
    return did


@pytest.fixture
def service_cleaning(admin_engine, clinic_a) -> uuid.UUID:
    return make_service(admin_engine, clinic_a, "cleaning", 30)


def make_service(admin_engine, clinic_id, name: str, duration_min: int,
                 price: int | None = None) -> uuid.UUID:
    sid = uuid.uuid4()
    with admin_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO service (id, clinic_id, name, duration_min, price) "
                 "VALUES (:id, :cid, :name, :dur, :price)"),
            {"id": sid, "cid": clinic_id, "name": name, "dur": duration_min,
             "price": price},
        )
    return sid


@pytest.fixture
def sched(app_session_factory, clinic_a):
    from navbat.scheduling.engine import SchedulingEngine

    return SchedulingEngine(app_session_factory, clinic_a)
