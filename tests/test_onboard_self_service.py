"""onboard-мутации для self-service инкремента 2 (вызываются из консоли)."""
from __future__ import annotations

import pytest
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


import uuid

from conftest import next_monday, at_tashkent


def _waitlist_status(admin_engine, wid):
    with admin_engine.begin() as conn:
        return conn.execute(text("SELECT status FROM waitlist WHERE id = :i"),
                            {"i": wid}).scalar_one()


def _add_waitlist(admin_engine, clinic_id, service_id, chat=555):
    with admin_engine.begin() as conn:
        return conn.execute(
            text("INSERT INTO waitlist (clinic_id, service_id, tg_chat_id) "
                 "VALUES (:c, :s, :chat) RETURNING id"),
            {"c": clinic_id, "s": service_id, "chat": chat}).scalar_one()


def test_deactivate_then_activate_doctor(app_session_factory, admin_engine,
                                         clinic_a):
    did = make_doctor(admin_engine, clinic_a, name="Akmal")
    onboard.deactivate_doctor(app_session_factory, clinic_a, did)
    assert _doctor_row(admin_engine, did).is_active is False
    onboard.activate_doctor(app_session_factory, clinic_a, did)
    assert _doctor_row(admin_engine, did).is_active is True


def test_deactivate_service_cancels_waitlist(app_session_factory, admin_engine,
                                             clinic_a):
    sid = make_service(admin_engine, clinic_a, "cleaning", 30)
    wid = _add_waitlist(admin_engine, clinic_a, sid)
    onboard.deactivate_service(app_session_factory, clinic_a, "cleaning")
    assert _service_row(admin_engine, clinic_a, "cleaning").is_active is False
    assert _waitlist_status(admin_engine, wid) == "cancelled"


def test_delete_unused_doctor_ok(app_session_factory, admin_engine, clinic_a):
    # ужесточение 27.07.2026 (ревью волны B, блокер 4): физическое удаление
    # требует, чтобы врач был СНАЧАЛА скрыт — инвариант жил только в UI
    # (кнопка удаления показывалась деактивированным), и путь подтверждения
    # его обходил, если сущность вернули в работу между экраном и нажатием
    did = make_doctor(admin_engine, clinic_a, name="Botir")
    onboard.deactivate_doctor(app_session_factory, clinic_a, did)
    onboard.delete_doctor(app_session_factory, clinic_a, did)
    with admin_engine.begin() as conn:
        gone = conn.execute(text("SELECT 1 FROM doctor WHERE id = :d"),
                            {"d": did}).scalar_one_or_none()
    assert gone is None


def test_delete_active_doctor_blocked(app_session_factory, admin_engine,
                                      clinic_a):
    did = make_doctor(admin_engine, clinic_a, name="Botir")
    with pytest.raises(ValueError, match="скройте"):
        onboard.delete_doctor(app_session_factory, clinic_a, did)


def test_delete_referenced_doctor_blocked(app_session_factory, admin_engine,
                                          clinic_a):
    # врача скрываем: иначе удаление отвергнет проверка is_active и тест
    # перестанет проверять свой инвариант — блокировку по ссылкам
    did = make_doctor(admin_engine, clinic_a, name="Akmal")
    sid = make_service(admin_engine, clinic_a, "cleaning", 30)
    day = next_monday()
    with admin_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO appointment (clinic_id, doctor_id, service_id, "
                 "status, time_range) VALUES (:c, :d, :s, 'booked', "
                 "tstzrange(:lo, :hi))"),
            {"c": clinic_a, "d": did, "s": sid,
             "lo": at_tashkent(day, "09:00"), "hi": at_tashkent(day, "09:30")})
    onboard.deactivate_doctor(app_session_factory, clinic_a, did)
    try:
        onboard.delete_doctor(app_session_factory, clinic_a, did)
        assert False, "удаление врача с записью должно быть запрещено"
    except ValueError as exc:
        assert "запис" in str(exc).lower()
