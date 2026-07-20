"""Миграция 0002: таблица conversation — хранение состояния диалога.

Уровень SQL: структура, RLS, уникальность чата в пределах клиники.
Persistence через DialogEngine добирается в тестах FSM.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from navbat.db.base import tenant_transaction


def _insert_conversation(session, chat_id: int, state: str = "idle") -> None:
    session.execute(
        text("INSERT INTO conversation (clinic_id, tg_chat_id, fsm_state, context) "
             "VALUES (current_setting('app.clinic_id')::uuid, :chat, :state, '{}')"),
        {"chat": chat_id, "state": state},
    )


def test_conversation_roundtrip(app_session_factory, clinic_a):
    with tenant_transaction(app_session_factory, clinic_a) as session:
        _insert_conversation(session, 100, "booking_collect")
    with tenant_transaction(app_session_factory, clinic_a) as session:
        row = session.execute(
            text("SELECT fsm_state, context FROM conversation WHERE tg_chat_id = 100")
        ).one()
    assert row.fsm_state == "booking_collect"
    assert row.context == {}


def test_conversation_rls_isolation(app_session_factory, clinic_a, clinic_b):
    with tenant_transaction(app_session_factory, clinic_a) as session:
        _insert_conversation(session, 100)
    with tenant_transaction(app_session_factory, clinic_b) as session:
        rows = session.execute(text("SELECT id FROM conversation")).all()
    assert rows == []


def test_conversation_unique_chat_per_clinic(app_session_factory, clinic_a, clinic_b):
    with tenant_transaction(app_session_factory, clinic_a) as session:
        _insert_conversation(session, 100)
    # тот же chat_id в другой клинике — допустим (уникальность в пределах клиники)
    with tenant_transaction(app_session_factory, clinic_b) as session:
        _insert_conversation(session, 100)
    with pytest.raises(IntegrityError):
        with tenant_transaction(app_session_factory, clinic_a) as session:
            _insert_conversation(session, 100)
