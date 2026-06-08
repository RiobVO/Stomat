"""Слой данных записей (appointments_repo) — прямое покрытие после
вынесения SQL из FSM (R1c)."""
from __future__ import annotations

from sqlalchemy import text

from conftest import at_tashkent, next_monday
from navbat.db.base import tenant_transaction
from navbat.dialog import appointments_repo, patients
from navbat.scheduling.engine import SchedulingEngine

CHAT = 5001


def _hold(app_session_factory, clinic_a, doctor_a, service_cleaning, hhmm="10:00",
          chat=CHAT):
    sched = SchedulingEngine(app_session_factory, clinic_a)
    return sched.hold(doctor_a, service_cleaning,
                      at_tashkent(next_monday(), hhmm), tg_chat_id=chat)


def test_active_by_chat_returns_nearest(app_session_factory, clinic_a, doctor_a,
                                        service_cleaning):
    appt = _hold(app_session_factory, clinic_a, doctor_a, service_cleaning)
    with tenant_transaction(app_session_factory, clinic_a) as s:
        row = appointments_repo.active_by_chat(s, CHAT)
        assert row.id == appt
        assert row.doctor_id == doctor_a
        assert appointments_repo.active_by_chat(s, 999_999) is None


def test_active_by_id_only_active(app_session_factory, clinic_a, doctor_a,
                                  service_cleaning):
    appt = _hold(app_session_factory, clinic_a, doctor_a, service_cleaning)
    with tenant_transaction(app_session_factory, clinic_a) as s:
        assert appointments_repo.active_by_id(s, str(appt)).id == appt
    # отменённая запись больше не активна
    SchedulingEngine(app_session_factory, clinic_a).cancel(appt)
    with tenant_transaction(app_session_factory, clinic_a) as s:
        assert appointments_repo.active_by_id(s, str(appt)) is None


def test_slot_bounds(app_session_factory, clinic_a, doctor_a, service_cleaning):
    appt = _hold(app_session_factory, clinic_a, doctor_a, service_cleaning)
    with tenant_transaction(app_session_factory, clinic_a) as s:
        row = appointments_repo.slot_bounds(s, appt)
        assert row.doctor_id == doctor_a
        assert row.start < row.finish


def test_set_patient(app_session_factory, clinic_a, doctor_a, service_cleaning):
    appt = _hold(app_session_factory, clinic_a, doctor_a, service_cleaning)
    with tenant_transaction(app_session_factory, clinic_a) as s:
        pid = patients.create_patient(s, CHAT, "Алишер", "901234567")
        appointments_repo.set_patient(s, appt, pid)
        row = appointments_repo.active_by_chat(s, CHAT)
        assert row.id == appt
    # запись с привязанным пациентом читается тем же запросом
    with tenant_transaction(app_session_factory, clinic_a) as s:
        bound = s.execute(
            text("SELECT patient_id FROM appointment WHERE id = :a"),
            {"a": appt}).scalar_one()
    assert bound == pid
