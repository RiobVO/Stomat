"""Хранение состояния диалога: одна строка conversation на чат в клинике.

Состояние переживает рестарт процесса; FSM-state и контекст пишутся
одним upsert внутри транзакции обработчика.

Контекст — типизированный DialogContext вместо «словаря со свалкой
строковых ключей»: забытое/опечатанное поле падает на доступе (AttributeError),
а не молча возвращает None. Поле extras хранит ключи, которыми управляют
адаптеры (telegram tg_actions и пр.): FSM их не читает, но обязан сохранять
при round-trip JSONB.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

# поля текущего сценария — чистятся по его завершении (_clear_booking);
# сессионные поля (lang, флаги показа) и extras при этом сохраняются
_BOOKING_FIELDS = (
    "service", "date", "time_ref", "doctor_id", "doctor_miss",
    "appointment_id", "slot_start", "slot_doctor", "pending_name",
    "resched_id", "resched_doctor", "cancel_id", "cancel_when", "cancel_via",
)

# поля с PII пациента — НЕ выносить в эскалацию админу (m1)
_PII_FIELDS = ("pending_name",)


@dataclass
class DialogContext:
    # сессия (переживает _clear_booking)
    lang: str | None = None
    greeting_shown: bool = False
    medical_shown: bool = False
    nlu_failures: int = 0
    # текущий сценарий записи/переноса/отмены (чистится по завершении)
    service: str | None = None
    date: str | None = None
    time_ref: str | None = None
    doctor_id: str | None = None
    doctor_miss: bool = False
    appointment_id: str | None = None
    slot_start: str | None = None
    slot_doctor: str | None = None
    pending_name: str | None = None
    resched_id: str | None = None
    resched_doctor: str | None = None
    cancel_id: str | None = None
    cancel_when: str | None = None
    cancel_via: str | None = None
    # ключи под управлением адаптеров (tg_actions и пр.) — passthrough
    extras: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict | None) -> "DialogContext":
        data = dict(data or {})
        known = {f.name for f in fields(cls) if f.name != "extras"}
        kwargs = {k: data.pop(k) for k in list(data) if k in known}
        return cls(extras=data, **kwargs)

    def to_dict(self) -> dict:
        """JSONB-представление: только непустые поля (+ extras), чтобы пустой
        контекст сериализовался в {} как раньше."""
        out = dict(self.extras)
        for f in fields(self):
            if f.name == "extras":
                continue
            value = getattr(self, f.name)
            if value != f.default:
                out[f.name] = value
        return out

    def clear_booking(self) -> None:
        """Сбросить поля текущего сценария, сохранив сессию и extras."""
        defaults = DialogContext()
        for name in _BOOKING_FIELDS:
            setattr(self, name, getattr(defaults, name))

    def escalation_dict(self) -> dict:
        """to_dict без PII пациента — для алерта админу (m1)."""
        data = self.to_dict()
        for name in _PII_FIELDS:
            data.pop(name, None)
        return data


@dataclass
class Conversation:
    chat_id: int
    state: str = "idle"
    context: DialogContext = field(default_factory=DialogContext)
    patient_id: str | None = None


def load_conversation(session: Session, chat_id: int) -> Conversation:
    row = session.execute(
        text("SELECT fsm_state, context, patient_id FROM conversation "
             "WHERE tg_chat_id = :chat"),
        {"chat": chat_id},
    ).one_or_none()
    if row is None:
        return Conversation(chat_id=chat_id)
    return Conversation(
        chat_id=chat_id,
        state=row.fsm_state,
        context=DialogContext.from_dict(row.context),
        patient_id=str(row.patient_id) if row.patient_id else None,
    )


def get_chat_lang(session: Session, chat_id: int | None) -> str:
    """Язык последнего диалога чата; нет разговора — ru."""
    if not chat_id:
        return "ru"
    lang = session.execute(
        text("SELECT context ->> 'lang' FROM conversation WHERE tg_chat_id = :chat"),
        {"chat": chat_id},
    ).scalar_one_or_none()
    return lang or "ru"


def save_conversation(session: Session, conv: Conversation) -> None:
    session.execute(
        text("""
            INSERT INTO conversation (clinic_id, tg_chat_id, fsm_state, context, patient_id)
            VALUES (current_setting('app.clinic_id')::uuid, :chat, :state,
                    CAST(:ctx AS jsonb), CAST(:patient AS uuid))
            ON CONFLICT (clinic_id, tg_chat_id) DO UPDATE
            SET fsm_state = EXCLUDED.fsm_state,
                context = EXCLUDED.context,
                patient_id = EXCLUDED.patient_id,
                updated_at = now()
        """),
        {"chat": conv.chat_id, "state": conv.state,
         "ctx": json.dumps(conv.context.to_dict(), ensure_ascii=False),
         "patient": conv.patient_id},
    )
