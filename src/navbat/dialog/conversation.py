"""Хранение состояния диалога: одна строка conversation на чат в клинике.

Состояние переживает рестарт процесса; FSM-state и контекст пишутся
одним upsert внутри транзакции обработчика.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass
class Conversation:
    chat_id: int
    state: str = "idle"
    context: dict = field(default_factory=dict)
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
        context=row.context,
        patient_id=str(row.patient_id) if row.patient_id else None,
    )


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
         "ctx": json.dumps(conv.context, ensure_ascii=False),
         "patient": conv.patient_id},
    )
