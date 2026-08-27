"""Отзывы: перехват негатива и сбор хороших оценок (инкремент 5).

После приёма бот спрашивает «Как всё прошло?» рядом звёзд: 4–5 → благодарность
со ссылкой клиники, 1–3 → нейтральная благодарность (алерт владельцу — Task 2).
Рассылка идёт reconciliation'ом из цикла напоминаний (состояние в БД,
переживает рестарт), одна просьба на приём (UNIQUE в review), кнопка несёт
приём сама — фоновый поток не трогает conversation пациента.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from navbat.dialog.replies import t
from navbat.onboard import set_clinic_review_url
from navbat.reminders import ReminderService
from test_dialog_booking import CHAT, RecordingNotifier, make_engine
from test_tg_worker import FakeTelegramAPI

TASHKENT = ZoneInfo("Asia/Tashkent")
REVIEW_URL = "https://g.page/r/shifo-dent/review"


def make_review_service(app_session_factory, clinic_id):
    api = FakeTelegramAPI()
    service = ReminderService(app_session_factory, clinic_id, tg_api=api,
                              notifier=RecordingNotifier())
    return service, api


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
