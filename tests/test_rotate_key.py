"""Ротация NAVBAT_ENC_KEY: перешифровка всех AES-полей одной транзакцией.

Сценарий — компрометация ключа: всё, что было шифровано старым, должно
читаться новым; сбой на любом значении — база не тронута (rollback).
"""
from __future__ import annotations

import base64
import uuid

import pytest
from sqlalchemy import text

from conftest import make_doctor
from navbat.crypto import decrypt_text, encrypt_text
from navbat.rotate_key import rotate

OLD_KEY = base64.b64encode(b"old-key-32-bytes-padded-0000000!").decode()
NEW_KEY = base64.b64encode(b"new-key-32-bytes-padded-0000000!").decode()


def _seed(admin_engine, clinic_id) -> uuid.UUID:
    """Клиника с токенами + пациент (имя и телефон), всё шифровано OLD_KEY."""
    patient_id = uuid.uuid4()
    with admin_engine.begin() as conn:
        conn.execute(text(
            "UPDATE clinic SET tg_bot_token_encrypted = :token, "
            "tg_webhook_secret_encrypted = :secret WHERE id = :id"),
            {"token": encrypt_text("123:BOT", key=OLD_KEY),
             "secret": encrypt_text("hook-secret", key=OLD_KEY),
             "id": clinic_id})
        conn.execute(text(
            "INSERT INTO patient (id, clinic_id, name_encrypted, phone_encrypted) "
            "VALUES (:id, :cid, :name, :phone)"),
            {"id": patient_id, "cid": clinic_id,
             "name": encrypt_text("Алишер", key=OLD_KEY),
             "phone": encrypt_text("998901234567", key=OLD_KEY)})
    return patient_id


def _patient_name(admin_engine, patient_id) -> str:
    with admin_engine.begin() as conn:
        return conn.execute(text(
            "SELECT name_encrypted FROM patient WHERE id = :id"),
            {"id": patient_id}).scalar_one()


def test_rotate_reencrypts_all_columns(admin_engine, clinic_a):
    patient_id = _seed(admin_engine, clinic_a)
    doctor_id = make_doctor(admin_engine, clinic_a)
    with admin_engine.begin() as conn:  # имя врача — старым ключом, не env-ключом
        conn.execute(text(
            "UPDATE doctor SET name_encrypted = :name WHERE id = :id"),
            {"name": encrypt_text("Доктор Зухра", key=OLD_KEY), "id": doctor_id})

    with admin_engine.begin() as conn:
        counts = rotate(conn, OLD_KEY, NEW_KEY)

    assert counts["clinic.tg_bot_token_encrypted"] == 1
    assert counts["clinic.tg_webhook_secret_encrypted"] == 1
    assert counts["clinic.gcal_refresh_token_encrypted"] == 0  # NULL — пропуск
    assert counts["doctor.name_encrypted"] == 1
    assert counts["patient.name_encrypted"] == 1
    assert counts["patient.phone_encrypted"] == 1

    token = _patient_name(admin_engine, patient_id)
    assert decrypt_text(token, key=NEW_KEY) == "Алишер"
    with pytest.raises(ValueError):
        decrypt_text(token, key=OLD_KEY)  # старый ключ больше не читает
    with admin_engine.begin() as conn:
        phone_token = conn.execute(text(
            "SELECT phone_encrypted FROM patient WHERE id = :id"),
            {"id": patient_id}).scalar_one()
    assert decrypt_text(phone_token, key=NEW_KEY) == "998901234567"


def test_rotate_is_idempotent(admin_engine, clinic_a):
    patient_id = _seed(admin_engine, clinic_a)
    with admin_engine.begin() as conn:
        rotate(conn, OLD_KEY, NEW_KEY)
    with admin_engine.begin() as conn:
        counts = rotate(conn, OLD_KEY, NEW_KEY)  # повторный прогон
    assert all(n == 0 for n in counts.values())  # всё уже новым ключом
    assert decrypt_text(_patient_name(admin_engine, patient_id),
                        key=NEW_KEY) == "Алишер"


def test_corrupted_value_aborts_whole_rotation(admin_engine, clinic_a):
    patient_id = _seed(admin_engine, clinic_a)
    with admin_engine.begin() as conn:
        conn.execute(text(
            "UPDATE clinic SET gcal_refresh_token_encrypted = 'garbage' "
            "WHERE id = :id"), {"id": clinic_a})

    with pytest.raises(ValueError, match="gcal_refresh_token_encrypted"):
        with admin_engine.begin() as conn:
            rotate(conn, OLD_KEY, NEW_KEY)

    # транзакция откатилась: пациент по-прежнему под старым ключом
    assert decrypt_text(_patient_name(admin_engine, patient_id),
                        key=OLD_KEY) == "Алишер"
