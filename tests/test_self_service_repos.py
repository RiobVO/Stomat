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


def _deactivate_service(admin_engine, clinic_id, name):
    with admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE service SET is_active = false "
                 "WHERE clinic_id = :c AND name = :n"),
            {"c": clinic_id, "n": name})


def test_service_keys_and_resolve_hide_inactive(app_session_factory,
                                                admin_engine, clinic_a):
    make_service(admin_engine, clinic_a, "cleaning", 30)
    make_service(admin_engine, clinic_a, "braces", 60)
    _deactivate_service(admin_engine, clinic_a, "braces")

    with tenant_transaction(app_session_factory, clinic_a) as session:
        keys = services_repo.service_keys(session)
        braces_id = services_repo.service_id(session, "braces")
        prices = {r.name for r in services_repo.price_list(session)}
    assert keys == ["cleaning"]
    assert braces_id is None  # деактивированную услугу не записать
    assert prices == {"cleaning"}


def test_service_list_all_shows_inactive(app_session_factory, admin_engine,
                                         clinic_a):
    make_service(admin_engine, clinic_a, "cleaning", 30, price=350000)
    make_service(admin_engine, clinic_a, "braces", 60)
    _deactivate_service(admin_engine, clinic_a, "braces")

    with tenant_transaction(app_session_factory, clinic_a) as session:
        rows = {r.name: r for r in services_repo.service_list_all(session)}
    assert rows["cleaning"].is_active is True
    assert rows["braces"].is_active is False
    assert rows["cleaning"].duration_min == 30
    assert rows["cleaning"].price == 350000


def test_deactivated_doctor_offers_no_slots(app_session_factory, admin_engine,
                                            clinic_a):
    from navbat.scheduling.engine import SchedulingEngine
    from conftest import next_monday

    only = make_doctor(admin_engine, clinic_a, name="Akmal")
    svc = make_service(admin_engine, clinic_a, "cleaning", 30)
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE doctor SET is_active = false WHERE id = :d"),
                     {"d": only})

    engine = SchedulingEngine(app_session_factory, clinic_a)
    with tenant_transaction(app_session_factory, clinic_a) as session:
        candidates = doctors_repo.doctor_list(session)
    assert candidates == []  # кандидатов нет → слотов не будет

    day = next_monday()
    slots = engine.find_free_slots(only, svc, day)  # сам врач ещё «жив» по id
    assert slots, "движок по id видит слоты — фильтрация на уровне списка врачей"
