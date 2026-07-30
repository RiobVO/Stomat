"""Durable-очередь апдейтов на Postgres: идемпотентность, per-chat порядок, ack.

Гарантии BRIEF: дубль webhook → одна строка; ack только после обработки;
сообщения чата — строго по update_id, чаты между собой — параллельно.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from navbat.db.base import tenant_transaction
from navbat.dialog.patients import contact_hash, normalize_phone
from navbat.telegram.queue import (
    claim_next,
    complete,
    enqueue,
    fail,
    reclaim_stale,
)

CHAT = 100
OTHER_CHAT = 200


def put(app_session_factory, clinic_id, update_id, chat_id=CHAT) -> bool:
    with tenant_transaction(app_session_factory, clinic_id) as session:
        return enqueue(session, update_id=update_id, tg_chat_id=chat_id,
                       payload={"update_id": update_id})


# ── Идемпотентность ──────────────────────────────────────────────────────────

def test_enqueue_duplicate_is_noop(app_session_factory, admin_engine, clinic_a):
    assert put(app_session_factory, clinic_a, 1) is True
    assert put(app_session_factory, clinic_a, 1) is False
    with admin_engine.begin() as conn:
        count = conn.execute(text("SELECT count(*) FROM message_queue")).scalar_one()
    assert count == 1


# ── Per-chat порядок ─────────────────────────────────────────────────────────

def test_claim_returns_lowest_update_id(app_session_factory, clinic_a):
    put(app_session_factory, clinic_a, 7)
    put(app_session_factory, clinic_a, 5)
    claimed = claim_next(app_session_factory, clinic_a)
    assert claimed is not None
    assert claimed.update_id == 5


def test_chat_is_serialized_while_processing(app_session_factory, clinic_a):
    put(app_session_factory, clinic_a, 1)
    put(app_session_factory, clinic_a, 2)
    first = claim_next(app_session_factory, clinic_a)
    assert first.update_id == 1
    # второй апдейт того же чата не выдаётся, пока первый не завершён
    assert claim_next(app_session_factory, clinic_a) is None

    with tenant_transaction(app_session_factory, clinic_a) as session:
        complete(session, first.id)
    second = claim_next(app_session_factory, clinic_a)
    assert second.update_id == 2


def test_other_chat_is_not_blocked(app_session_factory, clinic_a):
    put(app_session_factory, clinic_a, 1, chat_id=CHAT)
    put(app_session_factory, clinic_a, 2, chat_id=OTHER_CHAT)
    first = claim_next(app_session_factory, clinic_a)
    other = claim_next(app_session_factory, clinic_a)
    assert {first.tg_chat_id, other.tg_chat_id} == {CHAT, OTHER_CHAT}


# ── Retry и dead letter ──────────────────────────────────────────────────────

def test_fail_returns_to_pending_until_attempts_exhausted(app_session_factory,
                                                          admin_engine, clinic_a):
    put(app_session_factory, clinic_a, 1)
    for attempt in range(1, 3):
        claimed = claim_next(app_session_factory, clinic_a)
        assert claimed is not None, f"попытка {attempt}: апдейт должен переклеймиться"
        with tenant_transaction(app_session_factory, clinic_a) as session:
            assert fail(session, claimed.id) == "pending"

    claimed = claim_next(app_session_factory, clinic_a)
    with tenant_transaction(app_session_factory, clinic_a) as session:
        assert fail(session, claimed.id) == "failed"  # 3-я попытка — в dead letter
    assert claim_next(app_session_factory, clinic_a) is None
    with admin_engine.begin() as conn:
        status = conn.execute(text("SELECT status FROM message_queue")).scalar_one()
    assert status == "failed"


def test_failed_update_does_not_block_chat(app_session_factory, clinic_a):
    put(app_session_factory, clinic_a, 1)
    put(app_session_factory, clinic_a, 2)
    for _ in range(3):
        claimed = claim_next(app_session_factory, clinic_a)
        with tenant_transaction(app_session_factory, clinic_a) as session:
            fail(session, claimed.id)
    # update 1 умер в dead letter — чат живёт дальше
    assert claim_next(app_session_factory, clinic_a).update_id == 2


# ── Реклейм зависших ─────────────────────────────────────────────────────────

def test_reclaim_stale_processing(app_session_factory, admin_engine, clinic_a):
    put(app_session_factory, clinic_a, 1)
    claimed = claim_next(app_session_factory, clinic_a)
    assert claim_next(app_session_factory, clinic_a) is None

    # воркер «умер»: claimed_at уезжает в прошлое
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE message_queue SET claimed_at = now() - interval '10 minutes'"))
    with tenant_transaction(app_session_factory, clinic_a) as session:
        assert reclaim_stale(session) == 1

    reclaimed = claim_next(app_session_factory, clinic_a)
    assert reclaimed is not None
    assert reclaimed.update_id == claimed.update_id


def test_reclaim_keeps_fresh_processing(app_session_factory, clinic_a):
    put(app_session_factory, clinic_a, 1)
    claim_next(app_session_factory, clinic_a)
    with tenant_transaction(app_session_factory, clinic_a) as session:
        assert reclaim_stale(session) == 0


# ── RLS ──────────────────────────────────────────────────────────────────────

def test_queue_is_tenant_isolated(app_session_factory, clinic_a, clinic_b):
    put(app_session_factory, clinic_a, 1)
    assert claim_next(app_session_factory, clinic_b) is None
    assert claim_next(app_session_factory, clinic_a) is not None


# ── PII: телефон контакта не лежит в очереди открытым ─────────────────────────


def put_contact_payload(app_session_factory, clinic_id, phone,
                        update_id=900, chat_id=CHAT) -> None:
    """Кладёт апдейт с кнопочным контактом (request_contact) в очередь."""
    payload = {"update_id": update_id,
               "message": {"chat": {"id": chat_id}, "from": {"id": chat_id},
                           "contact": {"phone_number": phone, "user_id": chat_id}}}
    with tenant_transaction(app_session_factory, clinic_id) as session:
        enqueue(session, update_id, chat_id, payload)


def stored_payload(admin_engine) -> dict:
    with admin_engine.begin() as conn:
        return conn.execute(text("SELECT payload FROM message_queue")).scalar_one()


def test_enqueue_redacts_contact_phone(app_session_factory, admin_engine, clinic_a):
    """Узбекский номер: в payload вместо открытого телефона — только хэш."""
    put_contact_payload(app_session_factory, clinic_a, "998901234567")

    payload = stored_payload(admin_engine)
    contact = payload["message"]["contact"]
    assert "phone_number" not in contact, "открытый номер не должен лежать в очереди"
    assert "998901234567" not in json.dumps(payload), "номер не утёк ни в одно поле"
    assert contact["phone_hash"] == contact_hash(
        normalize_phone("998901234567"), "test-salt")


def test_enqueue_marks_non_uz_contact_invalid(app_session_factory, admin_engine,
                                               clinic_a):
    """Не-узбекский номер: открытый номер вырезан, хэша нет (воркер эскалирует)."""
    put_contact_payload(app_session_factory, clinic_a, "+79161234567")

    payload = stored_payload(admin_engine)
    contact = payload["message"]["contact"]
    assert "phone_number" not in contact
    assert "phone_hash" not in contact
    assert "79161234567" not in json.dumps(payload)
