"""Recall: возврат пациентов через N месяцев после визита (инкремент 4).

Пациент, которому не напомнили, не возвращается сам: чистка раз в полгода —
деньги клиники, о которых никто не спрашивает. Рассылка идёт reconciliation'ом
из цикла напоминаний (состояние в БД, переживает рестарт), одна отправка на
приём (UNIQUE в recall_outreach), приглашение несёт исходную запись в сырой
кнопке — фоновый поток не трогает conversation пациента.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from conftest import at_tashkent, next_monday
from navbat.db.base import tenant_transaction
from navbat.dialog.replies import service_label, t
from navbat.reminders import ReminderService
from navbat.retention import cleanup_old_data
from navbat.stats import collect_stats, render_stats
from test_dialog_booking import (CHAT, RecordingNotifier, make_engine,
                                 slot_buttons)
from test_tg_worker import (FakeTelegramAPI, make_worker, put_callback,
                            put_message)

TASHKENT = ZoneInfo("Asia/Tashkent")
ADMIN_CHAT = 777
RECALL_PRICE = 250_000


def make_recall_service(app_session_factory, clinic_id):
    api = FakeTelegramAPI()
    service = ReminderService(app_session_factory, clinic_id, tg_api=api,
                              notifier=RecordingNotifier())
    return service, api


def at_local(hour: int) -> datetime:
    """«Сегодня в HH:00» по часам клиники: окно рассылки живёт по её локали,
    а не по часам сервера."""
    return datetime.now(TASHKENT).replace(hour=hour, minute=0, second=0,
                                          microsecond=0)


def set_recall(admin_engine, service_id, months: int | None) -> None:
    with admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE service SET recall_months = :m WHERE id = :i"),
            {"m": months, "i": service_id})


def seed_past(admin_engine, clinic_id, doctor_id, service_id, months_ago,
              chat_id=CHAT, lang="ru") -> uuid.UUID:
    """Завершённый приём N месяцев назад прямым INSERT: движок не даёт занять
    прошедшее время, а recall'у нужен именно прошлый визит."""
    appointment_id = uuid.uuid4()
    with admin_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO appointment (id, clinic_id, doctor_id, "
                 "service_id, time_range, status, tg_chat_id, lang) "
                 "SELECT :i, :c, :d, :s, "
                 "       tstzrange(q.lo, q.lo + interval '30 minutes', '[)'), "
                 "       'booked', :chat, :lang "
                 "FROM (SELECT now() - make_interval(months => :m) AS lo) q"),
            {"i": appointment_id, "c": clinic_id, "d": doctor_id,
             "s": service_id, "chat": chat_id, "lang": lang,
             "m": months_ago})
    return appointment_id


def seed_future(admin_engine, clinic_id, doctor_id, service_id,
                chat_id=CHAT) -> uuid.UUID:
    """Будущая booked-запись того же чата: пациенту, который уже придёт,
    приглашение не нужно."""
    appointment_id = uuid.uuid4()
    start = at_tashkent(next_monday(), "10:00")
    with admin_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO appointment (id, clinic_id, doctor_id, "
                 "service_id, time_range, status, tg_chat_id) "
                 "VALUES (:i, :c, :d, :s, tstzrange(:lo, :hi, '[)'), "
                 "        'booked', :chat)"),
            {"i": appointment_id, "c": clinic_id, "d": doctor_id,
             "s": service_id, "lo": start,
             "hi": start + timedelta(minutes=30), "chat": chat_id})
    return appointment_id


def outreach_rows(admin_engine, clinic_id):
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT appointment_id, tg_chat_id, lang, booked_at "
                 "FROM recall_outreach WHERE clinic_id = :c ORDER BY id"),
            {"c": clinic_id}).all()


def conversation_count(admin_engine) -> int:
    with admin_engine.begin() as conn:
        return conn.execute(text("SELECT count(*) FROM conversation")).scalar()


def conversation_context(admin_engine, chat_id=CHAT) -> dict:
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT context FROM conversation WHERE tg_chat_id = :c"),
            {"c": chat_id}).scalar_one()


def set_price(admin_engine, service_id, price: int) -> None:
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE service SET price = :p WHERE id = :i"),
                     {"p": price, "i": service_id})


def recall_months(admin_engine, clinic_id, name="cleaning"):
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT recall_months FROM service WHERE clinic_id = :c "
                 "AND name = :n"), {"c": clinic_id, "n": name}).scalar_one()


def insert_outreach(admin_engine, clinic_id, appointment_id,
                    days_ago: int) -> None:
    """Строка журнала напрямую: ретеншену нужен возраст отправки, а не
    сценарий, которым она появилась."""
    with admin_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO recall_outreach (clinic_id, appointment_id, "
                 "tg_chat_id, lang, sent_at) VALUES (:c, :a, :chat, 'ru', "
                 "now() - make_interval(days => :d))"),
            {"c": clinic_id, "a": appointment_id, "chat": CHAT, "d": days_ago})


# ── Отправка приглашения ────────────────────────────────────────────────────

def test_recall_invite_sent_in_window(app_session_factory, admin_engine,
                                      clinic_a, doctor_a, service_cleaning):
    """Приём 7 месяцев назад при интервале 6 → приглашение на языке ПРИЁМА
    с сырой кнопкой записи, отправка отмечена в журнале."""
    set_recall(admin_engine, service_cleaning, 6)
    appointment_id = seed_past(admin_engine, clinic_a, doctor_a,
                               service_cleaning, months_ago=7, lang="uz")
    service, api = make_recall_service(app_session_factory, clinic_a)

    assert service.send_recalls(now_local=at_local(12)) == 1

    chat, body, buttons = api.sent[-1]
    assert chat == CHAT
    # язык берётся из appointment.lang: в conversation фоновый поток не
    # заглядывает и тем более его не переписывает
    assert body == t("recall_invite", "uz",
                     service=service_label("cleaning", "uz"), months=6)
    assert [b.label for b in buttons] == [t("btn_recall_book", "uz")]
    # кнопка сырая и несёт СВОЙ субъект: приглашение живёт дольше одной
    # отправки, номерная a:N потерялась бы при следующей карте кнопок
    assert [b.action for b in buttons] == [f"rcl:{appointment_id}"]
    assert all(len(b.action.encode()) <= 64 for b in buttons), \
        "лимит callback_data Telegram"
    assert [(r.appointment_id, r.tg_chat_id, r.lang, r.booked_at)
            for r in outreach_rows(admin_engine, clinic_a)] == \
        [(appointment_id, CHAT, "uz", None)]
    assert conversation_count(admin_engine) == 0, \
        "правило фона: рассылка не создаёт и не трогает диалог пациента"


def test_second_cycle_does_not_repeat_invite(app_session_factory, admin_engine,
                                             clinic_a, doctor_a,
                                             service_cleaning):
    """Цикл идёт каждые 30 секунд — второе приглашение по тому же приёму
    было бы спамом."""
    set_recall(admin_engine, service_cleaning, 6)
    seed_past(admin_engine, clinic_a, doctor_a, service_cleaning, months_ago=7)
    service, api = make_recall_service(app_session_factory, clinic_a)

    assert service.send_recalls(now_local=at_local(12)) == 1
    assert service.send_recalls(now_local=at_local(12)) == 0
    assert len(api.sent) == 1
    assert len(outreach_rows(admin_engine, clinic_a)) == 1


# ── Кого не беспокоим ───────────────────────────────────────────────────────

def test_future_appointment_silences_recall(app_session_factory, admin_engine,
                                            clinic_a, doctor_a,
                                            service_cleaning):
    """Пациент уже записан — звать его на приём нелепо."""
    set_recall(admin_engine, service_cleaning, 6)
    seed_past(admin_engine, clinic_a, doctor_a, service_cleaning, months_ago=7)
    seed_future(admin_engine, clinic_a, doctor_a, service_cleaning)
    service, api = make_recall_service(app_session_factory, clinic_a)

    assert service.send_recalls(now_local=at_local(12)) == 0
    assert api.sent == []
    assert outreach_rows(admin_engine, clinic_a) == []


def test_forgotten_patient_is_not_disturbed(app_session_factory, admin_engine,
                                            clinic_a, doctor_a,
                                            service_cleaning):
    """/forget обнуляет appointment.tg_chat_id — «не беспокоить» бесплатно."""
    set_recall(admin_engine, service_cleaning, 6)
    seed_past(admin_engine, clinic_a, doctor_a, service_cleaning, months_ago=7,
              chat_id=None)
    service, api = make_recall_service(app_session_factory, clinic_a)

    assert service.send_recalls(now_local=at_local(12)) == 0
    assert api.sent == []
    assert outreach_rows(admin_engine, clinic_a) == []


def test_night_is_silent_and_leaves_nothing_behind(app_session_factory,
                                                   admin_engine, clinic_a,
                                                   doctor_a, service_cleaning):
    """23:00 локали: пациента будить нельзя, а журнал должен остаться пустым —
    иначе утренний цикл сочтёт приглашение отправленным."""
    set_recall(admin_engine, service_cleaning, 6)
    seed_past(admin_engine, clinic_a, doctor_a, service_cleaning, months_ago=7)
    service, api = make_recall_service(app_session_factory, clinic_a)

    assert service.send_recalls(now_local=at_local(23)) == 0
    assert api.sent == []
    assert outreach_rows(admin_engine, clinic_a) == []


def test_service_without_interval_never_recalls(app_session_factory,
                                                admin_engine, clinic_a,
                                                doctor_a, service_cleaning):
    """recall_months IS NULL — рассылка по услуге выключена (умолчание)."""
    seed_past(admin_engine, clinic_a, doctor_a, service_cleaning, months_ago=7)
    service, api = make_recall_service(app_session_factory, clinic_a)

    assert service.send_recalls(now_local=at_local(12)) == 0
    assert api.sent == []
    assert outreach_rows(admin_engine, clinic_a) == []


def test_interval_not_reached_yet(app_session_factory, admin_engine, clinic_a,
                                  doctor_a, service_cleaning):
    """Три месяца из шести — рано: интервал услуги и есть повод написать."""
    set_recall(admin_engine, service_cleaning, 6)
    seed_past(admin_engine, clinic_a, doctor_a, service_cleaning, months_ago=3)
    service, api = make_recall_service(app_session_factory, clinic_a)

    assert service.send_recalls(now_local=at_local(12)) == 0
    assert api.sent == []
    assert outreach_rows(admin_engine, clinic_a) == []


# ── Тап по приглашению: запись на ту же услугу ──────────────────────────────

def test_invitation_tap_starts_booking_with_the_visit_service(
        app_session_factory, admin_engine, clinic_a, doctor_a,
        service_cleaning):
    """Тап по приглашению — запись на ТУ ЖЕ услугу: бот сразу спрашивает день,
    а не начинает с «какая услуга». Услуга берётся из приёма, а не из
    контекста: приглашение висит днями и переживает чужой сценарий."""
    appointment_id = seed_past(admin_engine, clinic_a, doctor_a,
                               service_cleaning, months_ago=7)
    engine = make_engine(app_session_factory, clinic_a, [])

    reply = engine.handle_action(CHAT, f"rcl:{appointment_id}")

    assert reply.text == t("ask_date", "ru"), reply.text
    assert any(b.action.startswith("date:") for b in reply.buttons), reply.buttons
    context = conversation_context(admin_engine)
    assert context.get("service") == "cleaning", context
    assert context.get("recall_source") == str(appointment_id), \
        "источник приглашения обязан дожить до confirm — по нему считается конверсия"


def test_urn_form_of_the_id_does_not_poison_the_transaction(
        app_session_factory, admin_engine, clinic_a, doctor_a,
        service_cleaning):
    """Валидатор uuid в Python ШИРЕ постгресового CAST: «urn:uuid:…» он
    принимает, а CAST(... AS uuid) на такой строке роняет транзакцию целиком
    (класс бага remind_cancel). В БД обязана уходить каноническая форма."""
    appointment_id = seed_past(admin_engine, clinic_a, doctor_a,
                               service_cleaning, months_ago=7)
    engine = make_engine(app_session_factory, clinic_a, [])

    reply = engine.handle_action(CHAT, f"rcl:urn:uuid:{appointment_id}")

    assert reply.text == t("ask_date", "ru"), reply.text


def test_foreign_invitation_button_books_nothing(app_session_factory,
                                                 admin_engine, clinic_a,
                                                 doctor_a, service_cleaning):
    """callback_data — вход от клиента: чужой приём в кнопке не должен
    записывать соседа (RLS изолирует клиники, но не пациентов)."""
    set_recall(admin_engine, service_cleaning, 6)
    stranger = seed_past(admin_engine, clinic_a, doctor_a, service_cleaning,
                         months_ago=7, chat_id=CHAT + 1)
    service, _ = make_recall_service(app_session_factory, clinic_a)
    assert service.send_recalls(now_local=at_local(12)) == 1
    engine = make_engine(app_session_factory, clinic_a, [])

    reply = engine.handle_action(CHAT, f"rcl:{stranger}")

    assert reply.text == t("stale_button", "ru"), reply.text
    assert "service" not in conversation_context(admin_engine), \
        "чужая кнопка завела пациента в запись по чужому визиту"
    assert [r.booked_at for r in outreach_rows(admin_engine, clinic_a)] == [None]


def test_broken_invitation_uuid_answers_instead_of_crashing(
        app_session_factory, admin_engine, clinic_a, doctor_a,
        service_cleaning):
    """Мусор в callback_data не должен падать в приведении типа: апдейт ушёл
    бы в dead letter вместо ответа пациенту."""
    engine = make_engine(app_session_factory, clinic_a, [])

    reply = engine.handle_action(CHAT, "rcl:не-uuid")

    assert reply.text == t("stale_button", "ru"), reply.text


# ── Конверсия: пациент вернулся по приглашению ──────────────────────────────

def test_return_by_invitation_reaches_the_showcase(
        app_session_factory, admin_engine, clinic_a, doctor_a,
        service_cleaning):
    """Главная цифра фичи: приглашение → запись. Полный путь от рассылки до
    подтверждения, а витрина обязана показать и приглашения, и деньги
    вернувшихся."""
    set_recall(admin_engine, service_cleaning, 6)
    set_price(admin_engine, service_cleaning, RECALL_PRICE)
    seed_past(admin_engine, clinic_a, doctor_a, service_cleaning, months_ago=7)
    service, api = make_recall_service(app_session_factory, clinic_a)
    assert service.send_recalls(now_local=at_local(12)) == 1
    invite = api.sent[-1][2][0].action

    engine = make_engine(app_session_factory, clinic_a, [])
    engine.handle_action(CHAT, invite)
    offer = engine.handle_action(CHAT, f"date:{next_monday().isoformat()}")
    engine.handle_action(CHAT, slot_buttons(offer)[0].action)
    engine.handle_text(CHAT, "Алишер")
    engine.handle_contact(CHAT, "998901234567", own=True)

    rows = outreach_rows(admin_engine, clinic_a)
    assert len(rows) == 1 and rows[0].booked_at is not None, \
        "конверсия не отмечена — витрина покажет ноль вернувшихся"
    assert "recall_source" not in conversation_context(admin_engine), \
        "источник пережил запись: следующая пойдёт в ту же конверсию"

    today = datetime.now(TASHKENT).date()
    with tenant_transaction(app_session_factory, clinic_a) as session:
        stats = collect_stats(session, today, today, TASHKENT)
    assert (stats.recalls_sent, stats.recalls_returned) == (1, 1)
    # деньги — цена услуги ИСХОДНОГО приёма: приглашение зовёт на неё же
    assert stats.recalls_saved == RECALL_PRICE
    assert "🔁" in render_stats(stats, today)


def test_ordinary_booking_does_not_steal_the_conversion(
        app_session_factory, admin_engine, clinic_a, doctor_a,
        service_cleaning):
    """Витрина не вправе выдавать обычную запись за возврат по приглашению:
    отметку ставит только сценарий, начатый с кнопки приглашения."""
    set_recall(admin_engine, service_cleaning, 6)
    seed_past(admin_engine, clinic_a, doctor_a, service_cleaning, months_ago=7)
    service, _ = make_recall_service(app_session_factory, clinic_a)
    assert service.send_recalls(now_local=at_local(12)) == 1

    engine = make_engine(app_session_factory, clinic_a, [])
    engine.handle_action(CHAT, "service:cleaning")  # своим ходом, мимо кнопки
    offer = engine.handle_action(CHAT, f"date:{next_monday().isoformat()}")
    engine.handle_action(CHAT, slot_buttons(offer)[0].action)
    engine.handle_text(CHAT, "Алишер")
    engine.handle_contact(CHAT, "998901234567", own=True)

    assert [r.booked_at for r in outreach_rows(admin_engine, clinic_a)] == [None]
    today = datetime.now(TASHKENT).date()
    with tenant_transaction(app_session_factory, clinic_a) as session:
        stats = collect_stats(session, today, today, TASHKENT)
    assert (stats.recalls_returned, stats.recalls_saved) == (0, 0)


def test_recall_line_appears_only_when_invitations_were_sent():
    """Витрина без нулей (конвенция /stats): строка возврата появляется,
    только когда рассылка работала."""
    from test_stats import mk_stats

    day = date(2026, 6, 11)
    loud = render_stats(mk_stats(recalls_sent=4, recalls_returned=1,
                                 recalls_saved=RECALL_PRICE), day)
    assert "🔁" in loud and "250 000" in loud, loud

    assert "🔁" not in render_stats(mk_stats(), day), \
        "пустая рассылка не должна занимать строку в сводке"


# ── Консоль: интервал у услуги ──────────────────────────────────────────────

def test_owner_switches_the_recall_interval_from_the_service_card(
        app_session_factory, admin_engine, clinic_a, service_cleaning):
    """Интервал — решение владельца, а не константа кода: включается и
    выключается из карточки услуги, без CLI."""
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)

    put_callback(app_session_factory, clinic_a, "adm:svc:cleaning:recall:6",
                 chat_id=ADMIN_CHAT)
    worker.process_one()

    assert recall_months(admin_engine, clinic_a) == 6
    card = api.edited[-1][2]
    assert "🔁" in card and "6 мес" in card, card

    put_callback(app_session_factory, clinic_a, "adm:svc:cleaning:recall:off",
                 chat_id=ADMIN_CHAT)
    worker.process_one()

    assert recall_months(admin_engine, clinic_a) is None
    assert "выкл" in api.edited[-1][2], api.edited[-1][2]


# ── Ретеншен ────────────────────────────────────────────────────────────────

def test_forget_takes_the_patient_out_of_the_journal(app_session_factory,
                                                     admin_engine, clinic_a):
    """/forget рвёт ВСЕ копии chat_id: журнал приглашений хранит свою и живёт
    полгода — «забытый» чат оставался бы в нём до срока (та же причина, по
    которой /forget сносит очередь ожидания и якоря свайп-ответа)."""
    worker, _, _ = make_worker(app_session_factory, clinic_a, [],
                               admin_chat_id=ADMIN_CHAT)
    insert_outreach(admin_engine, clinic_a, uuid.uuid4(), days_ago=1)

    put_message(app_session_factory, clinic_a, f"/forget {CHAT}",
                chat_id=ADMIN_CHAT)
    worker.process_one()

    assert outreach_rows(admin_engine, clinic_a) == []


def test_retention_forgets_closed_outreach(app_session_factory, admin_engine,
                                           clinic_a):
    """Окно конверсии давно закрыто, а в строке — chat_id пациента: журнал
    приглашений живёт своим сроком, как якоря свайп-ответа."""
    old, fresh = uuid.uuid4(), uuid.uuid4()
    insert_outreach(admin_engine, clinic_a, old, days_ago=200)
    insert_outreach(admin_engine, clinic_a, fresh, days_ago=10)

    cleanup_old_data(app_session_factory, clinic_a)

    assert [r.appointment_id for r in outreach_rows(admin_engine, clinic_a)] \
        == [fresh]
