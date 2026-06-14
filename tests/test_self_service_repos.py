"""Контракт is_active: деактивированный врач/услуга исчезают из записи и
слотов; *_all-варианты видят всех. Репозитории — единственная точка фильтра."""
from __future__ import annotations

import uuid

from sqlalchemy import text

from conftest import WORKING_INTERVALS, make_doctor, make_service
from navbat.db.base import tenant_transaction
from navbat.dialog import doctors_repo, services_repo


def _deactivate_doctor(admin_engine, doctor_id):
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE doctor SET is_active = false WHERE id = :d"),
                     {"d": doctor_id})


def test_doctor_list_hides_inactive(app_session_factory, admin_engine, clinic_a):
    active = make_doctor(admin_engine, clinic_a, name="Akmal")
    hidden = make_doctor(admin_engine, clinic_a, name="Botir")
    _deactivate_doctor(admin_engine, hidden)

    with tenant_transaction(app_session_factory, clinic_a) as session:
        ids = [row[0] for row in doctors_repo.doctor_list(session)]
    assert active in ids and hidden not in ids


def test_working_intervals_hides_inactive(app_session_factory, admin_engine,
                                          clinic_a):
    make_doctor(admin_engine, clinic_a, name="Akmal")
    hidden = make_doctor(admin_engine, clinic_a, name="Botir")
    _deactivate_doctor(admin_engine, hidden)

    with tenant_transaction(app_session_factory, clinic_a) as session:
        schedules = doctors_repo.working_intervals(session)
    assert len(schedules) == 1  # только активный врач формирует окно клиники


def test_doctor_list_all_shows_inactive(app_session_factory, admin_engine,
                                        clinic_a):
    active = make_doctor(admin_engine, clinic_a, name="Akmal")
    hidden = make_doctor(admin_engine, clinic_a, name="Botir")
    _deactivate_doctor(admin_engine, hidden)

    with tenant_transaction(app_session_factory, clinic_a) as session:
        rows = {r.id: r for r in doctors_repo.doctor_list_all(session)}
    assert rows[active].is_active is True and rows[hidden].is_active is False
    assert rows[active].name == "Akmal"  # имя расшифровано
