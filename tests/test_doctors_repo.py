"""Слой данных врачей (doctors_repo) — прямое покрытие после вынесения
SQL из FSM (R1b)."""
from __future__ import annotations

from conftest import make_doctor
from navbat.db.base import tenant_transaction
from navbat.dialog import doctors_repo


def test_working_intervals_one_per_doctor(app_session_factory, clinic_a,
                                          admin_engine):
    make_doctor(admin_engine, clinic_a)
    make_doctor(admin_engine, clinic_a)
    with tenant_transaction(app_session_factory, clinic_a) as s:
        intervals = doctors_repo.working_intervals(s)
    assert len(intervals) == 2
    # JSON-график: должен быть пригоден для open_bounds (непустой словарь)
    assert all(iv for iv in intervals)


def test_doctor_list_decrypts_name(app_session_factory, clinic_a, admin_engine):
    named = make_doctor(admin_engine, clinic_a, name="Доктор Хаус")
    make_doctor(admin_engine, clinic_a, name=None)
    with tenant_transaction(app_session_factory, clinic_a) as s:
        doctors = doctors_repo.doctor_list(s)
    by_id = {d[0]: d[1] for d in doctors}
    assert by_id[named] == "Доктор Хаус"
    assert len(doctors) == 2
    assert None in by_id.values()  # врач без имени -> None
