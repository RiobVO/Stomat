"""Пациенты: нормализация телефона, contact_hash, шифрование имени, find/create.

Ключ шифрования в тестах фиксированный (NAVBAT_ENC_KEY ставит фикстура).
"""
from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import text

from navbat.crypto import decrypt_text, encrypt_text
from navbat.db.base import tenant_transaction
from navbat.dialog.patients import (
    contact_hash,
    create_patient,
    find_patient_by_chat,
    normalize_phone,
)


# ── Телефон ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "+998 90 123-45-67",
    "998901234567",
    "90 123 45 67",
    "901234567",
    "+998901234567",
])
def test_normalize_phone_variants(raw):
    assert normalize_phone(raw) == "998901234567"


@pytest.mark.parametrize("raw", ["12345", "", "abc", "7901234567890"])
def test_normalize_phone_rejects_garbage(raw):
    with pytest.raises(ValueError):
        normalize_phone(raw)


def test_contact_hash_is_salted_sha256():
    expected = hashlib.sha256("998901234567соль".encode()).hexdigest()
    assert contact_hash("998901234567", "соль") == expected


# ── Шифрование имени ─────────────────────────────────────────────────────────

def test_encrypt_roundtrip():
    assert decrypt_text(encrypt_text("Алишер Усманов")) == "Алишер Усманов"


def test_encrypt_unique_nonce():
    assert encrypt_text("Алишер") != encrypt_text("Алишер")


def test_decrypt_garbage_raises():
    with pytest.raises(ValueError):
        decrypt_text("не-шифртекст")


# ── БД ───────────────────────────────────────────────────────────────────────

def test_create_and_find_patient(app_session_factory, admin_engine, clinic_a):
    with tenant_transaction(app_session_factory, clinic_a) as session:
        pid = create_patient(session, tg_chat_id=100, name="Алишер",
                             phone="+998 90 123-45-67")
    with tenant_transaction(app_session_factory, clinic_a) as session:
        patient = find_patient_by_chat(session, tg_chat_id=100)
    assert patient is not None
    assert patient.id == pid
    assert patient.name == "Алишер"

    # в БД — шифртекст и хеш, не плейнтекст (смотрим админом мимо RLS)
    with admin_engine.begin() as conn:
        row = conn.execute(
            text("SELECT name_encrypted, contact_hash FROM patient WHERE id = :id"),
            {"id": pid},
        ).one()
    assert "Алишер" not in row.name_encrypted
    assert "998901234567" not in row.contact_hash


def test_find_patient_missing_chat(app_session_factory, clinic_a):
    with tenant_transaction(app_session_factory, clinic_a) as session:
        assert find_patient_by_chat(session, tg_chat_id=42) is None
