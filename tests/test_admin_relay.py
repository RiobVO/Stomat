"""Якоря свайп-ответов админа: таблица admin_relay + запись при алертах.

Админ отвечает пациенту реплаем (Telegram reply) на алерт эскалации, а Bot API
отдаёт только id сообщения, на которое ответили. Связь «сообщение в админ-чате
→ пациент» держит якорь: парсинг chat_id из текста алерта отвергнут (шаблоны
двуязычные и меняются).
"""
from __future__ import annotations

from sqlalchemy import text

from navbat.db.base import tenant_transaction
from navbat.telegram import relay_repo
from navbat.telegram.escalation import TelegramEscalation, build_escalation

from test_tg_worker import FakeTelegramAPI

ADMIN_A, ADMIN_B = 7001, 7002
PATIENT = 5555


def anchors(app_session_factory, clinic_id) -> list:
    with tenant_transaction(app_session_factory, clinic_id) as session:
        return list(session.execute(text(
            "SELECT admin_chat_id, message_id, patient_chat_id "
            "FROM admin_relay ORDER BY id")).all())


# ── Запись якорей при алертах ───────────────────────────────────────────────

def test_escalation_alert_leaves_an_anchor_in_every_admin_chat(
        app_session_factory, clinic_a):
    """Реплаить на алерт может ЛЮБОЙ админ-чат, значит якорь нужен на каждое
    отправленное сообщение — со своим message_id: он уникален внутри чата,
    и один общий увёл бы ответ не тому пациенту."""
    api = FakeTelegramAPI()
    escalation = build_escalation(api, app_session_factory, clinic_a,
                                  [ADMIN_A, ADMIN_B])

    assert escalation.notify(PATIENT, "пациент просит человека", {}) is True

    assert [chat for chat, _, _ in api.sent] == [ADMIN_A, ADMIN_B]
    rows = anchors(app_session_factory, clinic_a)
    assert [(row.admin_chat_id, row.patient_chat_id) for row in rows] == \
        [(ADMIN_A, PATIENT), (ADMIN_B, PATIENT)]
    assert len({row.message_id for row in rows}) == 2, \
        "у каждой отправки свой message_id — якорь обязан взять его из ответа API"
    with tenant_transaction(app_session_factory, clinic_a) as session:
        assert relay_repo.patient_for(session, rows[0].admin_chat_id,
                                      rows[0].message_id) == PATIENT


def test_notify_without_writer_still_delivers(app_session_factory, clinic_a):
    """CLI и тесты без БД собирают нотификатор напрямую — поведение прежнее:
    якорить некуда, но алерт обязан уйти всем админ-чатам."""
    api = FakeTelegramAPI()
    escalation = TelegramEscalation(api, admin_chat_id=[ADMIN_A, ADMIN_B])

    assert escalation.notify(PATIENT, "пациент просит человека", {}) is True

    assert [chat for chat, _, _ in api.sent] == [ADMIN_A, ADMIN_B]
    assert anchors(app_session_factory, clinic_a) == []


def test_anchor_failure_does_not_swallow_the_alert(caplog):
    """Сигнал важнее якоря: эскалация — сигнал, а не транзакция. Потерять
    «пациент просит человека» хуже, чем потерять возможность ответить свайпом
    (у админа остаются /release и прямой чат), но след в логах обязателен."""
    api = FakeTelegramAPI()

    def broken_writer(admin_chat_id, message_id, patient_chat_id):
        raise RuntimeError("база недоступна")

    escalation = TelegramEscalation(api, admin_chat_id=[ADMIN_A, ADMIN_B],
                                    anchor_writer=broken_writer)

    with caplog.at_level("ERROR", logger="navbat.escalation"):
        assert escalation.notify(PATIENT, "просит человека", {}) is True

    assert len(api.sent) == 2, "сбой якоря не должен гасить рассылку"
    assert caplog.records, "сбой якоря обязан оставить след"


def test_only_escalation_alerts_are_anchored(app_session_factory, clinic_a):
    """🟡 FYI, операционные и системные алерты — не пациентские каналы:
    реплай на них адресовать некому."""
    api = FakeTelegramAPI()
    escalation = build_escalation(api, app_session_factory, clinic_a, [ADMIN_A])

    escalation.notify_fyi(PATIENT, "нет слотов", {})
    escalation.notify_ops("синхронизация не работает", {})
    escalation.notify_system("бэкапы БД не снимаются", {})

    assert anchors(app_session_factory, clinic_a) == []


# ── Репозиторий якорей ──────────────────────────────────────────────────────

def test_anchor_round_trip(app_session_factory, clinic_a):
    with tenant_transaction(app_session_factory, clinic_a) as session:
        relay_repo.save_anchor(session, ADMIN_A, 42, PATIENT)

    with tenant_transaction(app_session_factory, clinic_a) as session:
        assert relay_repo.patient_for(session, ADMIN_A, 42) == PATIENT
        assert relay_repo.patient_for(session, ADMIN_A, 43) is None, \
            "реплай на сообщение без якоря не должен уйти случайному пациенту"
        assert relay_repo.patient_for(session, ADMIN_B, 42) is None, \
            "message_id уникален только внутри чата — искать надо по паре"


def test_repeated_anchor_is_not_an_error(app_session_factory, clinic_a):
    """Повтор отправки того же алерта (ретрай транспорта) не должен ронять
    рассылку на UNIQUE: адресат тот же."""
    with tenant_transaction(app_session_factory, clinic_a) as session:
        relay_repo.save_anchor(session, ADMIN_A, 42, PATIENT)
        relay_repo.save_anchor(session, ADMIN_A, 42, PATIENT)
        assert relay_repo.patient_for(session, ADMIN_A, 42) == PATIENT


def test_cleanup_removes_only_stale_anchors(app_session_factory, admin_engine,
                                            clinic_a):
    with tenant_transaction(app_session_factory, clinic_a) as session:
        relay_repo.save_anchor(session, ADMIN_A, 42, PATIENT)
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE admin_relay SET created_at = now() - "
                          "interval '8 days' WHERE message_id = 42"))
    with tenant_transaction(app_session_factory, clinic_a) as session:
        relay_repo.save_anchor(session, ADMIN_A, 43, PATIENT)

    with tenant_transaction(app_session_factory, clinic_a) as session:
        deleted = relay_repo.cleanup_old(session, days=7)

    assert deleted == 1
    with tenant_transaction(app_session_factory, clinic_a) as session:
        assert relay_repo.patient_for(session, ADMIN_A, 42) is None
        assert relay_repo.patient_for(session, ADMIN_A, 43) == PATIENT


def test_anchors_are_isolated_by_clinic(app_session_factory, clinic_a, clinic_b):
    """Якорь — пара chat_id: утечка из чужой клиники отправила бы ответ
    администратора пациенту другой клиники."""
    with tenant_transaction(app_session_factory, clinic_a) as session:
        relay_repo.save_anchor(session, ADMIN_A, 42, PATIENT)

    with tenant_transaction(app_session_factory, clinic_b) as session:
        assert relay_repo.patient_for(session, ADMIN_A, 42) is None
