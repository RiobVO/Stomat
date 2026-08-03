"""Отзывы: перехват негатива и сбор хороших оценок (инкремент 5).

После приёма бот спрашивает «Как всё прошло?» рядом звёзд: 4–5 → благодарность
со ссылкой клиники, 1–3 → нейтральная благодарность пациенту и мгновенный алерт
владельцу вместо публичного негатива. Рассылка идёт reconciliation'ом из цикла
напоминаний (состояние в БД, переживает рестарт), одна просьба на приём
(UNIQUE в review), кнопка несёт приём сама — фоновый поток не трогает
conversation пациента.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from navbat.db.base import tenant_transaction
from navbat.dialog.conversation import load_conversation, save_conversation
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.replies import service_label, t
from navbat.nlu.extractor import FakeExtractor
from navbat.onboard import set_clinic_review_url
from navbat.reminders import ReminderService
from navbat.retention import cleanup_old_data
from navbat.stats import collect_stats, render_stats
from navbat.telegram import feed_repo
from navbat.telegram.escalation import build_escalation
from test_dialog_booking import (CHAT, RecordingNotifier, extr, fsm_state,
                                 make_engine)
from test_tg_worker import FakeTelegramAPI, make_worker, put_message

TASHKENT = ZoneInfo("Asia/Tashkent")
REVIEW_URL = "https://g.page/r/shifo-dent/review"
ADMIN_RU, ADMIN_UZ, ADMIN_CHAT = 8101, 8102, 8103


def make_review_service(app_session_factory, clinic_id):
    api = FakeTelegramAPI()
    service = ReminderService(app_session_factory, clinic_id, tg_api=api,
                              notifier=RecordingNotifier())
    return service, api


def make_alerting_engine(app_session_factory, clinic_id, admin_chats,
                         script=()):
    """Диалог с НАСТОЯЩИМ телеграм-нотификатором: алерт об оценке шлёт он,
    фейк-нотификатор соседних тестов канала алерта не имеет."""
    api = FakeTelegramAPI()
    engine = DialogEngine(app_session_factory, clinic_id,
                          extractor=FakeExtractor(script=list(script)),
                          notifier=build_escalation(api, app_session_factory,
                                                    clinic_id, admin_chats))
    return engine, api


def at_local(hour: int) -> datetime:
    """«Сегодня в HH:00» по часам клиники: окно рассылки живёт по её локали,
    а не по часам сервера."""
    return datetime.now(TASHKENT).replace(hour=hour, minute=0, second=0,
                                          microsecond=0)


def seed_visit(admin_engine, clinic_id, doctor_id, service_id, hours_ago,
               chat_id=CHAT, lang="ru", source="bot") -> uuid.UUID:
    """Приём, ЗАКОНЧИВШИЙСЯ hours_ago часов назад, прямым INSERT: движок не
    даёт занять прошедшее время, а просьбу об оценке рождает именно
    состоявшийся визит."""
    appointment_id = uuid.uuid4()
    with admin_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO appointment (id, clinic_id, doctor_id, "
                 "service_id, time_range, status, tg_chat_id, lang, source) "
                 "SELECT :i, :c, :d, :s, "
                 "       tstzrange(q.hi - interval '30 minutes', q.hi, '[)'), "
                 "       'booked', :chat, :lang, :src "
                 "FROM (SELECT now() - make_interval(hours => :h) AS hi) q"),
            {"i": appointment_id, "c": clinic_id, "d": doctor_id,
             "s": service_id, "chat": chat_id, "lang": lang, "h": hours_ago,
             "src": source})
    return appointment_id


def review_rows(admin_engine, clinic_id):
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT appointment_id, tg_chat_id, lang, rating, rated_at "
                 "FROM review WHERE clinic_id = :c ORDER BY id"),
            {"c": clinic_id}).all()


def conversation_count(admin_engine) -> int:
    with admin_engine.begin() as conn:
        return conn.execute(text("SELECT count(*) FROM conversation")).scalar()


def star_buttons(api):
    """Ряд звёзд последней отправки: они уходят одним рядом button_rows."""
    return api.row_keyboards[-1]


def insert_review(admin_engine, clinic_id, appointment_id, rating=None,
                  days_ago=0, chat_id=CHAT) -> None:
    """Строка журнала напрямую: витрине и ретеншену нужны оценка и возраст
    просьбы, а не сценарий, которым она появилась (паттерн insert_outreach)."""
    with admin_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO review (clinic_id, appointment_id, tg_chat_id, "
                 "                    lang, rating, requested_at, rated_at) "
                 "SELECT :c, :a, :chat, 'ru', q.rating, q.moment, "
                 # молчащая просьба: оценки нет — значит нет и времени ответа
                 "       CASE WHEN q.rating IS NULL THEN NULL "
                 "            ELSE q.moment END "
                 "FROM (SELECT now() - make_interval(days => :d) AS moment, "
                 "             CAST(:r AS int) AS rating) q"),
            {"c": clinic_id, "a": appointment_id, "chat": chat_id,
             "r": rating, "d": days_ago})


def set_admin_lang(app_session_factory, clinic_id, chat_id, lang) -> None:
    """Язык консоли админ-чата: алерт приходит владельцу на нём (карта, №16)."""
    with tenant_transaction(app_session_factory, clinic_id) as session:
        conv = load_conversation(session, chat_id)
        conv.context.extras["adm_lang"] = lang
        save_conversation(session, conv)


def anchors(admin_engine):
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT admin_chat_id, message_id, patient_chat_id "
                 "FROM admin_relay ORDER BY id")).all()


def visit_when(admin_engine, appointment_id) -> str:
    """Начало приёма глазами владельца — в локали клиники, как в алерте."""
    with admin_engine.begin() as conn:
        start = conn.execute(
            text("SELECT lower(time_range) FROM appointment WHERE id = :i"),
            {"i": appointment_id}).scalar_one()
    return f"{start.astimezone(TASHKENT):%d.%m %H:%M}"


# ── Просьба об оценке ───────────────────────────────────────────────────────

def test_review_request_sent_in_window(app_session_factory, admin_engine,
                                       clinic_a, doctor_a, service_cleaning):
    """Приём закончился 3 часа назад → просьба на языке ПРИЁМА с рядом из пяти
    сырых кнопок-звёзд; отправка отмечена в журнале."""
    appointment_id = seed_visit(admin_engine, clinic_a, doctor_a,
                                service_cleaning, hours_ago=3, lang="uz")
    service, api = make_review_service(app_session_factory, clinic_a)

    assert service.send_review_requests(now_local=at_local(12)) == 1

    chat, body, _ = api.sent[-1]
    assert chat == CHAT
    # язык берётся из appointment.lang: в conversation фоновый поток не
    # заглядывает и тем более его не переписывает
    assert body == t("review_ask", "uz")
    rows = star_buttons(api)
    assert len(rows) == 1, "пять звёзд обязаны лечь ОДНИМ рядом"
    # кнопки сырые и несут СВОЙ приём: просьба висит в чате, а карта tg_actions
    # одна на чат и перезаписывается любой следующей отправкой кнопок
    assert [b.action for b in rows[0]] == \
        [f"rate:{appointment_id}:{n}" for n in range(1, 6)]
    assert all(len(b.action.encode()) <= 64 for b in rows[0]), \
        "лимит callback_data Telegram"
    assert [(r.appointment_id, r.tg_chat_id, r.lang, r.rating, r.rated_at)
            for r in review_rows(admin_engine, clinic_a)] == \
        [(appointment_id, CHAT, "uz", None, None)]
    assert conversation_count(admin_engine) == 0, \
        "правило фона: рассылка не создаёт и не трогает диалог пациента"


def test_second_cycle_does_not_repeat_the_request(app_session_factory,
                                                  admin_engine, clinic_a,
                                                  doctor_a, service_cleaning):
    """Цикл идёт каждые 30 секунд — вторая просьба по тому же приёму была бы
    спамом, а вторая строка review сломала бы «одна оценка на приём»."""
    seed_visit(admin_engine, clinic_a, doctor_a, service_cleaning, hours_ago=3)
    service, api = make_review_service(app_session_factory, clinic_a)

    assert service.send_review_requests(now_local=at_local(12)) == 1
    assert service.send_review_requests(now_local=at_local(12)) == 0
    assert len(api.sent) == 1
    assert len(review_rows(admin_engine, clinic_a)) == 1


# ── Кого не спрашиваем ──────────────────────────────────────────────────────

def test_visit_three_days_ago_is_not_asked(app_session_factory, admin_engine,
                                           clinic_a, doctor_a,
                                           service_cleaning):
    """Через трое суток спрашивать поздно: пациент уже не помнит приём, а
    просьба выглядит как спам из ниоткуда."""
    seed_visit(admin_engine, clinic_a, doctor_a, service_cleaning, hours_ago=72)
    service, api = make_review_service(app_session_factory, clinic_a)

    assert service.send_review_requests(now_local=at_local(12)) == 0
    assert api.sent == []
    assert review_rows(admin_engine, clinic_a) == []


def test_visit_an_hour_ago_is_too_fresh(app_session_factory, admin_engine,
                                        clinic_a, doctor_a, service_cleaning):
    """Час назад — пациент может быть ещё в кресле (приём затянулся) или в
    дороге: спрашивать «как всё прошло?» рано."""
    seed_visit(admin_engine, clinic_a, doctor_a, service_cleaning, hours_ago=1)
    service, api = make_review_service(app_session_factory, clinic_a)

    assert service.send_review_requests(now_local=at_local(12)) == 0
    assert api.sent == []
    assert review_rows(admin_engine, clinic_a) == []


def test_forgotten_patient_is_not_asked(app_session_factory, admin_engine,
                                        clinic_a, doctor_a, service_cleaning):
    """/forget обнуляет appointment.tg_chat_id — «не беспокоить» бесплатно."""
    seed_visit(admin_engine, clinic_a, doctor_a, service_cleaning, hours_ago=3,
               chat_id=None)
    service, api = make_review_service(app_session_factory, clinic_a)

    assert service.send_review_requests(now_local=at_local(12)) == 0
    assert api.sent == []
    assert review_rows(admin_engine, clinic_a) == []


def test_showcase_history_is_not_asked(app_session_factory, admin_engine,
                                       clinic_a, doctor_a, service_cleaning):
    """Витрина показа (`onboard --demo-history`) сеет прошлые приёмы со
    служебными chat_id: без фильтра по источнику бот прямо во время показа
    слал бы просьбы десятками в несуществующие чаты — ровно та причина, по
    которой сидер не сеет очередь ожидания."""
    seed_visit(admin_engine, clinic_a, doctor_a, service_cleaning, hours_ago=3,
               chat_id=-900_000_001, source="demo_history")
    service, api = make_review_service(app_session_factory, clinic_a)

    assert service.send_review_requests(now_local=at_local(12)) == 0
    assert api.sent == []
    assert review_rows(admin_engine, clinic_a) == []


def test_night_is_silent_and_leaves_nothing_behind(app_session_factory,
                                                   admin_engine, clinic_a,
                                                   doctor_a, service_cleaning):
    """23:00 локали: вечерний приём не повод будить пациента, а журнал должен
    остаться пустым — иначе утренний цикл сочтёт просьбу отправленной."""
    seed_visit(admin_engine, clinic_a, doctor_a, service_cleaning, hours_ago=3)
    service, api = make_review_service(app_session_factory, clinic_a)

    assert service.send_review_requests(now_local=at_local(23)) == 0
    assert api.sent == []
    assert review_rows(admin_engine, clinic_a) == []


def test_batch_stops_when_the_window_closes(app_session_factory, admin_engine,
                                            clinic_a, doctor_a,
                                            service_cleaning):
    """Гейт 09–21 на входе держит только НАЧАЛО пачки: отправки идут по сети
    (ретраи Bot API, десятки просьб) и успевают выехать за 21:00. Невзятые
    строки ждут утра, поэтому отметки в журнале у них быть не должно: с ней их
    пропустят навсегда."""
    first = seed_visit(admin_engine, clinic_a, doctor_a, service_cleaning,
                       hours_ago=5)
    seed_visit(admin_engine, clinic_a, doctor_a, service_cleaning, hours_ago=3,
               chat_id=CHAT + 1)
    service, api = make_review_service(app_session_factory, clinic_a)

    def clock() -> datetime:
        # стрелки переваливают за 21:00 ровно после первой отправки; привязка
        # к факту отправки, а не к числу вызовов, — иначе тест ломается от
        # любой лишней сверки момента внутри цикла
        if api.sent:
            return at_local(21) + timedelta(seconds=1)
        return at_local(20) + timedelta(minutes=59, seconds=59)

    assert service.send_review_requests(now_local=clock) == 1
    assert len(api.sent) == 1, "рассылка вышла за окно и разбудила пациента"
    assert [r.appointment_id for r in review_rows(admin_engine, clinic_a)] \
        == [first], "невзятая строка отмечена как спрошенная — утром её пропустят"


# ── Тап по звезде ───────────────────────────────────────────────────────────

def test_good_rating_thanks_with_the_clinic_link(app_session_factory,
                                                 admin_engine, clinic_a,
                                                 doctor_a, service_cleaning):
    """Пятёрка — это публичный отзыв, ради которого фича и нужна: пациент
    получает ссылку клиники, оценка ложится в журнал, кнопки гаснут (edit)."""
    appointment_id = seed_visit(admin_engine, clinic_a, doctor_a,
                                service_cleaning, hours_ago=3)
    service, _ = make_review_service(app_session_factory, clinic_a)
    assert service.send_review_requests(now_local=at_local(12)) == 1
    set_clinic_review_url(app_session_factory, clinic_a, REVIEW_URL)
    engine = make_engine(app_session_factory, clinic_a, [])

    reply = engine.handle_action(CHAT, f"rate:{appointment_id}:5")

    assert reply.text == t("review_thanks_good", "ru") + "\n\n" \
        + t("review_link_line", "ru", url=REVIEW_URL), reply.text
    assert reply.edit is True, "кнопки-звёзды обязаны погаснуть на месте"
    assert not reply.buttons and not reply.button_rows, \
        "погашенное сообщение не должно нести звёзды заново"
    row = review_rows(admin_engine, clinic_a)[0]
    assert (row.rating, row.rated_at is not None) == (5, True)


def test_good_rating_without_link_is_still_a_thank_you(app_session_factory,
                                                       admin_engine, clinic_a,
                                                       doctor_a,
                                                       service_cleaning):
    """Клиника не завела review_url (умолчание) — строки со ссылкой быть не
    должно, но оценка засчитана и пациент поблагодарён."""
    appointment_id = seed_visit(admin_engine, clinic_a, doctor_a,
                                service_cleaning, hours_ago=3)
    service, _ = make_review_service(app_session_factory, clinic_a)
    assert service.send_review_requests(now_local=at_local(12)) == 1
    engine = make_engine(app_session_factory, clinic_a, [])

    reply = engine.handle_action(CHAT, f"rate:{appointment_id}:4")

    assert reply.text == t("review_thanks_good", "ru"), reply.text
    assert review_rows(admin_engine, clinic_a)[0].rating == 4


def test_bad_rating_answers_the_patient_neutrally(app_session_factory,
                                                  admin_engine, clinic_a,
                                                  doctor_a, service_cleaning):
    """1–3 — сигнал владельцу, а не пациенту: публичной ссылки он не получает,
    ответ нейтральный, оценка сохранена."""
    appointment_id = seed_visit(admin_engine, clinic_a, doctor_a,
                                service_cleaning, hours_ago=3)
    service, _ = make_review_service(app_session_factory, clinic_a)
    assert service.send_review_requests(now_local=at_local(12)) == 1
    set_clinic_review_url(app_session_factory, clinic_a, REVIEW_URL)
    engine = make_engine(app_session_factory, clinic_a, [])

    reply = engine.handle_action(CHAT, f"rate:{appointment_id}:2")

    assert reply.text == t("review_thanks_bad", "ru"), reply.text
    assert REVIEW_URL not in reply.text, \
        "недовольного пациента нельзя звать писать публичный отзыв"
    assert reply.edit is True
    assert review_rows(admin_engine, clinic_a)[0].rating == 2


def test_second_tap_does_not_overwrite_the_rating(app_session_factory,
                                                  admin_engine, clinic_a,
                                                  doctor_a, service_cleaning):
    """Оценка одна на приём: второй тап отвечает toast'ом и НЕ переписывает
    первую — иначе владелец увидел бы не то, что пациент нажал первым."""
    appointment_id = seed_visit(admin_engine, clinic_a, doctor_a,
                                service_cleaning, hours_ago=3)
    service, _ = make_review_service(app_session_factory, clinic_a)
    assert service.send_review_requests(now_local=at_local(12)) == 1
    engine = make_engine(app_session_factory, clinic_a, [])
    engine.handle_action(CHAT, f"rate:{appointment_id}:5")
    first = review_rows(admin_engine, clinic_a)[0]

    reply = engine.handle_action(CHAT, f"rate:{appointment_id}:1")

    assert reply.toast == t("review_already", "ru"), reply
    assert reply.text == "", "toast'а достаточно — второе сообщение в чат лишнее"
    assert reply.edit is False, "гасить нечего: кнопки погашены первым тапом"
    row = review_rows(admin_engine, clinic_a)[0]
    assert (row.rating, row.rated_at) == (5, first.rated_at)


def test_foreign_star_rates_nothing(app_session_factory, admin_engine,
                                    clinic_a, doctor_a, service_cleaning):
    """callback_data — вход от клиента: чужой приём в кнопке не должен ставить
    оценку соседу (RLS изолирует клиники, но не пациентов)."""
    stranger = seed_visit(admin_engine, clinic_a, doctor_a, service_cleaning,
                          hours_ago=3, chat_id=CHAT + 1)
    service, _ = make_review_service(app_session_factory, clinic_a)
    assert service.send_review_requests(now_local=at_local(12)) == 1
    engine = make_engine(app_session_factory, clinic_a, [])

    reply = engine.handle_action(CHAT, f"rate:{stranger}:5")

    assert reply.text == t("stale_button", "ru"), reply.text
    assert reply.toast is None, "чужая кнопка — не «уже учтено»"
    assert [r.rating for r in review_rows(admin_engine, clinic_a)] == [None]


def test_garbage_in_the_star_button_answers_instead_of_crashing(
        app_session_factory, admin_engine, clinic_a, doctor_a,
        service_cleaning):
    """Мусор в callback_data не должен ронять обработку: битый uuid уводил бы
    апдейт в dead letter вместо ответа, а оценка вне 1..5 разбивалась бы о
    CHECK в БД, отравляя транзакцию целиком."""
    appointment_id = seed_visit(admin_engine, clinic_a, doctor_a,
                                service_cleaning, hours_ago=3)
    service, _ = make_review_service(app_session_factory, clinic_a)
    assert service.send_review_requests(now_local=at_local(12)) == 1
    engine = make_engine(app_session_factory, clinic_a, [])

    assert engine.handle_action(CHAT, "rate:не-uuid:5").text == \
        t("stale_button", "ru")
    assert engine.handle_action(CHAT, f"rate:{appointment_id}:9").text == \
        t("stale_button", "ru")
    assert engine.handle_action(CHAT, f"rate:{appointment_id}:0").text == \
        t("stale_button", "ru")

    assert [r.rating for r in review_rows(admin_engine, clinic_a)] == [None]


# ── Алерт владельцу: перехват негатива ──────────────────────────────────────

def test_bad_rating_alerts_every_admin_chat(app_session_factory, admin_engine,
                                            clinic_a, doctor_a,
                                            service_cleaning):
    """Смысл фичи: недовольный не уходит писать в Google молча — владелец
    узнаёт об оценке в ту же секунду, на своём языке и с якорем свайп-ответа,
    чтобы ответить пациенту прямо из алерта."""
    appointment_id = seed_visit(admin_engine, clinic_a, doctor_a,
                                service_cleaning, hours_ago=3)
    service, _ = make_review_service(app_session_factory, clinic_a)
    assert service.send_review_requests(now_local=at_local(12)) == 1
    set_admin_lang(app_session_factory, clinic_a, ADMIN_UZ, "uz")
    engine, api = make_alerting_engine(app_session_factory, clinic_a,
                                       [ADMIN_RU, ADMIN_UZ],
                                       [extr(intent="other")])

    reply = engine.handle_action(CHAT, f"rate:{appointment_id}:2")

    assert reply.text == t("review_thanks_bad", "ru"), reply.text
    assert [chat for chat, _, _ in api.sent] == [ADMIN_RU, ADMIN_UZ], \
        "алерт уходит веером всем админ-чатам клиники, по одному разу"
    sent = {chat: body for chat, body, _ in api.sent}
    when = visit_when(admin_engine, appointment_id)
    for body in sent.values():
        assert body.startswith("⭐2"), body
        assert str(CHAT) in body, "владельцу нужен чат, кому звонить: " + body
        assert when in body, body
    # язык каждого чата решается при отправке (карта, №16)
    assert service_label("cleaning", "ru") in sent[ADMIN_RU], sent[ADMIN_RU]
    assert service_label("cleaning", "uz") in sent[ADMIN_UZ], sent[ADMIN_UZ]
    assert sent[ADMIN_RU] != sent[ADMIN_UZ], "узбекский чат получил русский алерт"
    rows = anchors(admin_engine)
    assert [(r.admin_chat_id, r.patient_chat_id) for r in rows] == \
        [(ADMIN_RU, CHAT), (ADMIN_UZ, CHAT)], \
        "без якоря свайп-ответ владельца не найдёт пациента"
    assert len({r.message_id for r in rows}) == 2, \
        "у каждой отправки свой message_id — якорь обязан взять его из ответа API"
    # пациента никто не замораживал: человека он не просил, а алерт — сигнал
    # владельцу, а не эскалация (иначе следующий текст получил бы «передаю
    # администратору» и /release стал бы обязательным)
    assert fsm_state(admin_engine) != "escalated"
    follow_up = engine.handle_text(CHAT, "спасибо, всё нормально")
    assert follow_up.text != t("escalated", "ru"), \
        "оценка заморозила пациента: его следующий текст ушёл в стоп-состояние"


def test_good_rating_does_not_disturb_the_owner(app_session_factory,
                                                admin_engine, clinic_a,
                                                doctor_a, service_cleaning):
    """Алерт существует ради негатива, который иначе уйдёт в публичный отзыв.
    Пятёрка — не повод дёргать владельца среди приёма."""
    appointment_id = seed_visit(admin_engine, clinic_a, doctor_a,
                                service_cleaning, hours_ago=3)
    service, _ = make_review_service(app_session_factory, clinic_a)
    assert service.send_review_requests(now_local=at_local(12)) == 1
    engine, api = make_alerting_engine(app_session_factory, clinic_a,
                                       [ADMIN_RU])

    engine.handle_action(CHAT, f"rate:{appointment_id}:5")

    assert api.sent == [], "хорошая оценка подняла владельца"
    assert anchors(admin_engine) == []


def test_broken_alert_data_does_not_poison_the_tap(app_session_factory,
                                                   admin_engine, clinic_a,
                                                   doctor_a, service_cleaning,
                                                   monkeypatch):
    """Данные алерта собираются ВНУТРИ транзакции тапа, и упавший SELECT травит
    её целиком (PostgreSQL 25P02): вместе с ним пропали бы и оценка, и
    conversation пациента. Сигнал владельцу дешевле ответа пациенту."""
    appointment_id = seed_visit(admin_engine, clinic_a, doctor_a,
                                service_cleaning, hours_ago=3)
    service, _ = make_review_service(app_session_factory, clinic_a)
    assert service.send_review_requests(now_local=at_local(12)) == 1
    engine, api = make_alerting_engine(app_session_factory, clinic_a,
                                       [ADMIN_RU])

    attempts = []

    def broken_card(session, appointment_id):
        # ровно то, чем кончается сломанный SELECT: транзакция aborted
        attempts.append(appointment_id)
        session.execute(text("SELECT 1 / 0"))

    monkeypatch.setattr(feed_repo, "appointment_card", broken_card)
    reply = engine.handle_action(CHAT, f"rate:{appointment_id}:2")

    assert attempts, "алерт не пытался собрать данные — сбой не воспроизведён"
    assert reply.text == t("review_thanks_bad", "ru"), reply.text
    assert api.sent == [], "данные алерта не собрались — слать нечего"
    row = review_rows(admin_engine, clinic_a)[0]
    assert (row.rating, row.rated_at is not None) == (2, True), \
        "оценка не доехала до БД: транзакция тапа отравлена сбоем алерта"
    assert fsm_state(admin_engine) == "idle", \
        "conversation сохранён: следующее сообщение продолжает диалог"


# ── Витрина владельца ───────────────────────────────────────────────────────

def test_stats_show_the_average_patient_rating(app_session_factory,
                                               admin_engine, clinic_a):
    """Средняя оценка за период — то, ради чего оценки и собираются. Считается
    по МОМЕНТУ ОЦЕНКИ: просьба могла уйти вчера, а неотвеченная не считается
    вовсе (иначе средняя падала бы от молчания)."""
    insert_review(admin_engine, clinic_a, uuid.uuid4(), rating=5)
    insert_review(admin_engine, clinic_a, uuid.uuid4(), rating=2)
    insert_review(admin_engine, clinic_a, uuid.uuid4())  # спросили, молчит

    today = datetime.now(TASHKENT).date()
    with tenant_transaction(app_session_factory, clinic_a) as session:
        stats = collect_stats(session, today, today, TASHKENT)

    assert (stats.reviews_count, stats.reviews_avg) == (2, 3.5)
    rendered = render_stats(stats, today)
    assert "⭐" in rendered and "средняя: 3.5 (из 2)" in rendered, rendered


def test_stats_hide_the_rating_line_without_ratings():
    """Витрина без нулей (конвенция /stats): никто не оценил — строки нет."""
    from test_stats import mk_stats

    assert "⭐" not in render_stats(mk_stats(), date(2026, 6, 11)), \
        "пустой журнал оценок занял строку в сводке"


# ── Гигиена: журнал оценок хранит свою копию chat_id ────────────────────────

def test_retention_forgets_old_reviews(app_session_factory, admin_engine,
                                       clinic_a):
    """В строке журнала — chat_id пациента: вечно она не живёт. Срок свой,
    полугодовой, как у журнала приглашений."""
    old, fresh = uuid.uuid4(), uuid.uuid4()
    insert_review(admin_engine, clinic_a, old, rating=5, days_ago=200)
    insert_review(admin_engine, clinic_a, fresh, rating=5, days_ago=10)

    cleanup_old_data(app_session_factory, clinic_a)

    assert [r.appointment_id for r in review_rows(admin_engine, clinic_a)] \
        == [fresh]


def test_forget_takes_the_patient_out_of_the_review_journal(
        app_session_factory, admin_engine, clinic_a):
    """/forget рвёт ВСЕ копии chat_id: журнал оценок хранит свою и живёт
    полгода — «забытый» чат оставался бы в нём до срока (та же причина, по
    которой /forget сносит журнал приглашений и якоря свайп-ответа)."""
    worker, _, _ = make_worker(app_session_factory, clinic_a, [],
                               admin_chat_id=ADMIN_CHAT)
    mine, stranger = uuid.uuid4(), uuid.uuid4()
    insert_review(admin_engine, clinic_a, mine, rating=2)
    insert_review(admin_engine, clinic_a, stranger, rating=2, chat_id=CHAT + 1)

    put_message(app_session_factory, clinic_a, f"/forget {CHAT}",
                chat_id=ADMIN_CHAT)
    worker.process_one()

    assert [r.appointment_id for r in review_rows(admin_engine, clinic_a)] \
        == [stranger], "оценки забытого чата пережили /forget"
