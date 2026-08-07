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
    """«+998 90 123-45-67» / «90 123 45 67» -> 998901234567.

    Номер любой страны принимается как есть (П-2в): из кнопки Telegram
    приходит подлинный номер аккаунта, страна — не повод для отказа.
    9 цифр — локальный узбекский без кода; 7–15 цифр (E.164) — как есть."""
    digits = _DIGITS_RE.sub("", raw)
    if len(digits) == 9:
        return "998" + digits
    if 7 <= len(digits) <= 15:
        return digits
    raise ValueError(f"не похоже на телефонный номер: {raw!r}")


def contact_hash(phone: str, salt: str) -> str:
    return hashlib.sha256((phone + salt).encode()).hexdigest()


def _clinic_salt(session: Session) -> str:
    return session.execute(
        text("SELECT salt FROM clinic WHERE id = current_setting('app.clinic_id')::uuid")
    ).scalar_one() or ""


def phone_to_hash(session: Session, raw_phone: str) -> str:
    """Сырой номер → SHA-256-хэш (нормализация к 998… + соль клиники).

    ValueError, если номер не приводится к узбекскому формату — вызывающий
    решает, что делать (хэшируется на границе enqueue, лид с не-узбекским
    номером уводится администратору).
    """
    return contact_hash(normalize_phone(raw_phone), _clinic_salt(session))


def create_patient(
    session: Session, tg_chat_id: int, name: str, phone: str
) -> uuid.UUID:
    return create_patient_with_hash(
        session, tg_chat_id, name, phone_to_hash(session, phone),
        encrypt_text(normalize_phone(phone)))


def create_patient_with_hash(
    session: Session, tg_chat_id: int, name: str, phone_hash: str,
    phone_encrypted: str | None = None,
) -> uuid.UUID:
    """Создаёт пациента с УЖЕ посчитанным хэшем телефона: открытый номер
    хэшируется на границе очереди и в durable-payload не сохраняется.
    phone_encrypted — AES-шифртекст номера оттуда же (пересмотр 11.06:
    номер нужен владельцу в событии календаря; паритет с именем)."""
    return session.execute(
        text("INSERT INTO patient (clinic_id, tg_chat_id, name_encrypted, "
             "contact_hash, phone_encrypted) "
             "VALUES (current_setting('app.clinic_id')::uuid, :chat, :name, "
             ":hash, :phone) RETURNING id"),
        {
            "chat": tg_chat_id,
            "name": encrypt_text(name),
            "hash": phone_hash,
            "phone": phone_encrypted,
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
