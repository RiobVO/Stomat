"""Пациенты: нормализация телефона, contact_hash, создание/поиск.

Конвенции BRIEF: телефон нормализуется к 998XXXXXXXXX ДО хеширования
(иначе дедуп/история рассыпаются), hash = SHA-256(phone + clinic.salt),
имя — шифртекст (navbat.crypto). Функции работают внутри tenant_transaction.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from navbat.crypto import decrypt_text, encrypt_text

_DIGITS_RE = re.compile(r"\D")


@dataclass(frozen=True)
class PatientRecord:
    id: uuid.UUID
    name: str | None


def normalize_phone(raw: str) -> str:
    """«+998 90 123-45-67» / «90 123 45 67» -> 998901234567."""
    digits = _DIGITS_RE.sub("", raw)
    if len(digits) == 9:
        return "998" + digits
    if len(digits) == 12 and digits.startswith("998"):
        return digits
    raise ValueError(f"не похоже на узбекский номер: {raw!r}")


def contact_hash(phone: str, salt: str) -> str:
    return hashlib.sha256((phone + salt).encode()).hexdigest()


def create_patient(
    session: Session, tg_chat_id: int, name: str, phone: str
) -> uuid.UUID:
    salt = session.execute(
        text("SELECT salt FROM clinic WHERE id = current_setting('app.clinic_id')::uuid")
    ).scalar_one() or ""
    return session.execute(
        text("INSERT INTO patient (clinic_id, tg_chat_id, name_encrypted, contact_hash) "
             "VALUES (current_setting('app.clinic_id')::uuid, :chat, :name, :hash) "
             "RETURNING id"),
        {
            "chat": tg_chat_id,
            "name": encrypt_text(name),
            "hash": contact_hash(normalize_phone(phone), salt),
        },
    ).scalar_one()


def find_patient_by_chat(session: Session, tg_chat_id: int) -> PatientRecord | None:
    row = session.execute(
        text("SELECT id, name_encrypted FROM patient WHERE tg_chat_id = :chat "
             "ORDER BY id LIMIT 1"),
        {"chat": tg_chat_id},
    ).one_or_none()
    if row is None:
        return None
    name = decrypt_text(row.name_encrypted) if row.name_encrypted else None
    return PatientRecord(id=row.id, name=name)
