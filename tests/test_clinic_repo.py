"""Слой данных настроек клиники (clinic_repo) — прямое покрытие после
вынесения SQL из FSM (R1b)."""
from __future__ import annotations

from datetime import date

from sqlalchemy import text

from navbat.db.base import tenant_transaction
from navbat.dialog import clinic_repo


def test_clinic_name_and_timezone(app_session_factory, clinic_a):
    with tenant_transaction(app_session_factory, clinic_a) as s:
        assert clinic_repo.clinic_name(s) == "Clinic A"
        assert clinic_repo.clinic_timezone(s) == "Asia/Tashkent"


def test_holidays_on(app_session_factory, clinic_a, admin_engine):
    with admin_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO holiday (clinic_id, date, reason) "
                 "VALUES (:cid, :day, 'Navruz')"),
            {"cid": clinic_a, "day": date(2026, 3, 21)},
        )
    with tenant_transaction(app_session_factory, clinic_a) as s:
        assert clinic_repo.holidays_on(s, date(2026, 3, 21)) == {date(2026, 3, 21)}
        assert clinic_repo.holidays_on(s, date(2026, 3, 22)) == set()
