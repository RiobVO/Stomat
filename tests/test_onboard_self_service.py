"""onboard-мутации для self-service инкремента 2 (вызываются из консоли)."""
from __future__ import annotations

from sqlalchemy import text

from conftest import make_doctor, make_service
from navbat import onboard
from navbat.crypto import decrypt_text


def _doctor_row(admin_engine, doctor_id):
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT name_encrypted, buffer_min, is_active "
                 "FROM doctor WHERE id = :d"), {"d": doctor_id}).one()


def _service_row(admin_engine, clinic_id, name):
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT duration_min, is_active FROM service "
                 "WHERE clinic_id = :c AND name = :n"),
            {"c": clinic_id, "n": name}).one()


def test_set_service_duration(app_session_factory, admin_engine, clinic_a):
    make_service(admin_engine, clinic_a, "cleaning", 30)
    onboard.set_service_duration(app_session_factory, clinic_a, "cleaning", 45)
    assert _service_row(admin_engine, clinic_a, "cleaning").duration_min == 45


def test_rename_doctor_encrypts(app_session_factory, admin_engine, clinic_a):
    did = make_doctor(admin_engine, clinic_a, name="Akmal")
    onboard.rename_doctor(app_session_factory, clinic_a, did, "Akmal Karimov")
    row = _doctor_row(admin_engine, did)
    assert decrypt_text(row.name_encrypted) == "Akmal Karimov"


def test_set_doctor_buffer(app_session_factory, admin_engine, clinic_a):
    did = make_doctor(admin_engine, clinic_a, name="Akmal", buffer_min=10)
    onboard.set_doctor_buffer(app_session_factory, clinic_a, did, 20)
    assert _doctor_row(admin_engine, did).buffer_min == 20
