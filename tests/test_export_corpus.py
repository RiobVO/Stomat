"""Экспорт обезличенного корпуса (data flywheel): только тексты пациентов,
PII замаскирована, админ-чаты/команды/кнопки/дубли отброшены."""
from __future__ import annotations

from sqlalchemy import text

from navbat.db.base import tenant_transaction
from navbat.export_corpus import MENU_LABELS, export_corpus
from navbat.telegram.queue import enqueue
from test_dialog_booking import CHAT
from test_tg_worker import UPDATE_SEQ, put_callback, put_message

ADMIN_CHAT = 777


def put_contact(app_session_factory, clinic_id, chat_id=CHAT):
    update_id = next(UPDATE_SEQ)
    payload = {"update_id": update_id,
               "message": {"chat": {"id": chat_id},
                           "from": {"id": chat_id},
                           "contact": {"phone_number": "+998901234567",
                                       "user_id": chat_id}}}
    with tenant_transaction(app_session_factory, clinic_id) as session:
        enqueue(session, update_id, chat_id, payload)


def finish_queue(admin_engine, clinic_id, admin_chat=ADMIN_CHAT):
    """Все апдейты → done (экспорт читает только done/failed) + админ-чат."""
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE message_queue SET status = 'done' "
                          "WHERE clinic_id = :c"), {"c": clinic_id})
        conn.execute(text("UPDATE clinic SET tg_admin_chat_ids = :a "
                          "WHERE id = :c"),
                     {"a": [admin_chat], "c": clinic_id})


def texts_of(records):
    return [r["text"] for r in records]


def test_export_only_text_messages(app_session_factory, admin_engine, clinic_a):
    put_message(app_session_factory, clinic_a, "Болит зуб, можно завтра?")
    put_contact(app_session_factory, clinic_a)
    put_callback(app_session_factory, clinic_a, "a:1")
    finish_queue(admin_engine, clinic_a)

    records, counts = export_corpus(app_session_factory, clinic_a)
    assert texts_of(records) == ["Болит зуб, можно завтра?"]
    assert counts["exported"] == 1


def test_export_excludes_admin_chats(app_session_factory, admin_engine, clinic_a):
    put_message(app_session_factory, clinic_a, "Сообщение пациента норм")
    put_message(app_session_factory, clinic_a, "/stats", chat_id=ADMIN_CHAT)
    put_message(app_session_factory, clinic_a, "ответ владельца текстом",
                chat_id=ADMIN_CHAT)
    finish_queue(admin_engine, clinic_a)

    records, _ = export_corpus(app_session_factory, clinic_a)
    assert texts_of(records) == ["Сообщение пациента норм"]


def test_export_redacts_phone_and_drops_identity(app_session_factory,
                                                 admin_engine, clinic_a):
    put_message(app_session_factory, clinic_a,
                "Перезвоните 998901234567, пишите @akmal_uz или t.me/clinic")
    finish_queue(admin_engine, clinic_a)

    records, _ = export_corpus(app_session_factory, clinic_a)
    body = records[0]["text"]
    assert "[phone]" in body and "998" not in body
    assert "[user]" in body and "akmal_uz" not in body
    assert "[link]" in body and "t.me" not in body
    # ничего, кроме обезличенного текста и служебных полей
    assert set(records[0]) == {"id", "text", "source", "category", "gold"}


def test_export_dedups_and_skips_commands_and_menu_labels(
        app_session_factory, admin_engine, clinic_a):
    label = next(iter(MENU_LABELS))
    put_message(app_session_factory, clinic_a, "/start")
    put_message(app_session_factory, clinic_a, label)
    put_message(app_session_factory, clinic_a, "Завтра можно прийти?")
    put_message(app_session_factory, clinic_a, "  завтра  МОЖНО прийти? ")
    finish_queue(admin_engine, clinic_a)

    records, counts = export_corpus(app_session_factory, clinic_a)
    assert texts_of(records) == ["Завтра можно прийти?"]
    assert (counts["command"], counts["menu_label"], counts["duplicate"]) \
        == (1, 1, 1)


def test_export_record_format(app_session_factory, admin_engine, clinic_a):
    put_message(app_session_factory, clinic_a, "Сколько стоит чистка?")
    finish_queue(admin_engine, clinic_a)

    records, _ = export_corpus(app_session_factory, clinic_a)
    rec = records[0]
    assert rec["id"] == "p_0001"
    assert rec["source"] == "pilot" and rec["category"] == "pilot_raw"
    assert rec["gold"] is None


def test_pending_updates_not_exported(app_session_factory, admin_engine,
                                      clinic_a):
    # необработанные (pending) апдейты — ещё живая очередь, не экспортируем
    put_message(app_session_factory, clinic_a, "ещё в обработке")
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE clinic SET tg_admin_chat_ids = '{}' "
                          "WHERE id = :c"), {"c": clinic_a})

    records, counts = export_corpus(app_session_factory, clinic_a)
    assert records == [] and counts["fetched"] == 0
