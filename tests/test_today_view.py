"""Экран «Сегодня» и утренняя сводка (пакет показа, инкремент 2).

Клиника без Google Calendar не видит свой день целиком: лента показывает
события по одному, а «что у нас сегодня» приходилось собирать в голове.
/today и кнопка консоли дают день одним экраном, утренняя сводка приносит
его в 08:30 сама — до открытия, пока день ещё можно перекроить.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from conftest import at_tashkent, make_doctor
from navbat.db.base import tenant_transaction
from navbat.dialog.conversation import load_conversation, save_conversation
from navbat.dialog.patients import create_patient
from navbat.dialog.replies import service_label
from navbat.reminders import ReminderService
from navbat.telegram.admin_texts import at
from test_admin_console import ADMIN_CHAT, click, last_to, send_admin
from test_dialog_booking import CHAT
from test_tg_worker import FakeTelegramAPI, make_worker

TASHKENT = ZoneInfo("Asia/Tashkent")
ADMIN_SECOND = ADMIN_CHAT + 1
DOCTOR = "Акмаль Каримов"
PATIENT = "Алишер"
PHONE = "901234567"
PHONE_STORED = "998901234567"  # normalize_phone: 9 цифр → узбекский код


@pytest.fixture
def doctor_named(admin_engine, clinic_a):
    """Врач с именем: в списке дня владелец обязан видеть, к кому идут."""
    return make_doctor(admin_engine, clinic_a, name=DOCTOR)


def today_local():
    """Сегодня по часам клиники, а не сервера."""
    return datetime.now(TASHKENT).date()


def add_patient(app_session_factory, clinic_id):
    with tenant_transaction(app_session_factory, clinic_id) as session:
        return create_patient(session, tg_chat_id=CHAT, name=PATIENT,
                              phone=PHONE)


def seed_today(admin_engine, clinic_id, doctor_id, service_id, hhmm,
               patient_id=None, chat_id=None):
    """Приём на сегодня прямым INSERT: движок не даёт занять прошедший час,
    а «сегодня» в тестах — любое время суток и любой день недели.

    Возвращает id — тестам статусов подтверждения нужна конкретная запись."""
    start = at_tashkent(today_local(), hhmm)
    appointment_id = uuid.uuid4()
    with admin_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO appointment (id, clinic_id, doctor_id, "
                 "service_id, patient_id, time_range, status, tg_chat_id) "
                 "VALUES (:i, :c, :d, :s, :p, tstzrange(:lo, :hi, '[)'), "
                 "'booked', :chat)"),
            {"i": appointment_id, "c": clinic_id, "d": doctor_id,
             "s": service_id, "p": patient_id, "lo": start,
             "hi": start + timedelta(minutes=30), "chat": chat_id})
    return appointment_id


def set_admin_lang(app_session_factory, clinic_id, lang, chat_id=ADMIN_CHAT):
    with tenant_transaction(app_session_factory, clinic_id) as session:
        conv = load_conversation(session, chat_id)
        conv.context.extras["adm_lang"] = lang
        save_conversation(session, conv)


def morning_mark(admin_engine):
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT last_morning_digest_date FROM clinic")).scalar_one()


# ── /today: день одним экраном ──────────────────────────────────────────────

def test_today_lists_appointments_in_time_order(
        app_session_factory, admin_engine, clinic_a, doctor_named,
        service_cleaning):
    """Главная ценность экрана: весь день по порядку, с телефоном под рукой.

    Вторая запись — без пациента (приём с улицы, /forget): заглушка не
    отменяет строку, иначе день на экране расходится с днём врача."""
    patient_id = add_patient(app_session_factory, clinic_a)
    # позднее время вставлено ПЕРВЫМ: порядок даёт запрос, а не вставка
    seed_today(admin_engine, clinic_a, doctor_named, service_cleaning, "14:00")
    seed_today(admin_engine, clinic_a, doctor_named, service_cleaning, "09:00",
               patient_id=patient_id, chat_id=CHAT)
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)

    send_admin(worker, app_session_factory, clinic_a, "/today")

    body = last_to(api, ADMIN_CHAT)
    assert body.index("09:00") < body.index("14:00"), body
    assert PATIENT in body and PHONE_STORED in body, body
    assert service_label("cleaning", "ru") in body, body
    assert DOCTOR in body, body
    assert at("feed_no_name", "ru") in body, \
        f"приём без пациента пропал из дня: {body}"


def test_today_without_appointments_says_so(app_session_factory, clinic_a,
                                            doctor_named, service_cleaning):
    """Пустой день — отдельный ответ, а не пустая шапка с нулём."""
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)

    send_admin(worker, app_session_factory, clinic_a, "/today")

    assert at("today_empty", "ru") in last_to(api, ADMIN_CHAT)


def test_today_speaks_the_language_of_the_admin_chat(
        app_session_factory, admin_engine, clinic_a, doctor_named,
        service_cleaning):
    """Узбекоязычный администратор читает день по-узбекски (карта, №16)."""
    seed_today(admin_engine, clinic_a, doctor_named, service_cleaning, "09:00")
    set_admin_lang(app_session_factory, clinic_a, "uz")
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)

    send_admin(worker, app_session_factory, clinic_a, "/today")

    body = last_to(api, ADMIN_CHAT)
    assert service_label("cleaning", "uz") in body, body
    assert service_label("cleaning", "ru") not in body, body


def test_console_button_shows_the_same_day(app_session_factory, admin_engine,
                                           clinic_a, doctor_named,
                                           service_cleaning):
    """Кнопка «Обновить» в экране дня перерисовывает его на месте: экран
    висит в чате часами, а записи за это время приходят и отменяются."""
    seed_today(admin_engine, clinic_a, doctor_named, service_cleaning, "09:00")
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)

    send_admin(worker, app_session_factory, clinic_a, "/today")
    body = last_to(api, ADMIN_CHAT)
    assert "adm:today" in [b.action for row in api.row_keyboards[-1]
                           for b in row], api.row_keyboards[-1]

    click(worker, app_session_factory, clinic_a, "adm:today")

    assert api.edited[-1][2] == body, api.edited[-1]


# ── Утренняя сводка ─────────────────────────────────────────────────────────

def morning_service(app_session_factory, clinic_id, api,
                    chats=(ADMIN_CHAT, ADMIN_SECOND)):
    return ReminderService(app_session_factory, clinic_id, tg_api=api,
                           digest_chat_id=chats)


def test_morning_summary_reaches_every_admin_chat(
        app_session_factory, admin_engine, clinic_a, doctor_named,
        service_cleaning):
    """08:30 — до открытия: владелец видит день, пока его ещё можно
    перекроить. Сводка идёт веером, как вечерний дайджест."""
    patient_id = add_patient(app_session_factory, clinic_a)
    seed_today(admin_engine, clinic_a, doctor_named, service_cleaning, "09:00",
               patient_id=patient_id, chat_id=CHAT)
    api = FakeTelegramAPI()
    service = morning_service(app_session_factory, clinic_a, api)
    moment = datetime.now(TASHKENT).replace(hour=8, minute=31)

    assert service.maybe_send_morning_summary(now_local=moment) is True

    got = {chat: body for chat, body, _ in api.sent}
    assert set(got) == {ADMIN_CHAT, ADMIN_SECOND}, got
    for body in got.values():
        assert body.startswith(at("morning_header", "ru")), body
        assert "09:00" in body and PATIENT in body, body
    assert morning_mark(admin_engine) == moment.date()


def test_morning_summary_silent_before_its_hour(
        app_session_factory, admin_engine, clinic_a, doctor_named,
        service_cleaning):
    """08:00 рано: сводку смотреть некому, и отметка дня не ставится —
    иначе она съест сегодняшнюю рассылку."""
    seed_today(admin_engine, clinic_a, doctor_named, service_cleaning, "09:00")
    api = FakeTelegramAPI()
    service = morning_service(app_session_factory, clinic_a, api)
    early = datetime.now(TASHKENT).replace(hour=8, minute=0)

    assert service.maybe_send_morning_summary(now_local=early) is False
    assert api.sent == []
    assert morning_mark(admin_engine) is None


def test_empty_day_marks_itself_without_a_message(
        app_session_factory, admin_engine, clinic_a, doctor_named,
        service_cleaning):
    """Нулём владельца не будим, но день отмечаем: цикл напоминаний тикает
    каждые 30 секунд и до полуночи ходил бы в базу за тем же пустым днём."""
    api = FakeTelegramAPI()
    service = morning_service(app_session_factory, clinic_a, api)
    moment = datetime.now(TASHKENT).replace(hour=8, minute=31)

    assert service.maybe_send_morning_summary(now_local=moment) is False
    assert api.sent == []
    assert morning_mark(admin_engine) == moment.date()


def test_morning_summary_goes_out_once_a_day(
        app_session_factory, admin_engine, clinic_a, doctor_named,
        service_cleaning):
    """Такт цикла — 30 секунд: без отметки владелец получал бы день заново
    каждые полминуты до самой ночи."""
    seed_today(admin_engine, clinic_a, doctor_named, service_cleaning, "09:00")
    api = FakeTelegramAPI()
    service = morning_service(app_session_factory, clinic_a, api)
    moment = datetime.now(TASHKENT).replace(hour=8, minute=31)

    assert service.maybe_send_morning_summary(now_local=moment) is True
    delivered = len(api.sent)

    assert service.maybe_send_morning_summary(now_local=moment) is False
    assert len(api.sent) == delivered, api.sent[delivered:]


# ── Статусы подтверждения визита (инкремент 3) ─────────────────────────────

def mark_confirmed(admin_engine, appointment_id):
    """Пациент нажал «Приду» — то же, что пишет ветка attend."""
    with admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE appointment SET confirm_status = 'confirmed', "
                 "confirmed_at = now() WHERE id = :id"), {"id": appointment_id})


def mark_reminded(admin_engine, clinic_id, appointment_id):
    """Напоминание по записи уже ушло: «молчит» — это отправленное
    напоминание без отметки, отдельного состояния у записи нет."""
    with admin_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO reminder (clinic_id, appointment_id, kind, "
                 "send_at, status, sent_at) "
                 "VALUES (:c, :a, '120m', now(), 'sent', now())"),
            {"c": clinic_id, "a": appointment_id})


def line_for(body, hhmm):
    return next(line for line in body.splitlines() if hhmm in line)


def three_states(admin_engine, clinic_a, doctor_id, service_id):
    """День из трёх записей: подтвердил / молчит / напоминание ещё не ушло."""
    confirmed = seed_today(admin_engine, clinic_a, doctor_id, service_id,
                           "09:00", chat_id=CHAT)
    silent = seed_today(admin_engine, clinic_a, doctor_id, service_id,
                        "10:00", chat_id=CHAT + 1)
    seed_today(admin_engine, clinic_a, doctor_id, service_id,
               "11:00", chat_id=CHAT + 2)  # напоминание ещё не уходило
    # подтвердивший тоже получил напоминание: отметка обязана перебивать ⏳
    mark_reminded(admin_engine, clinic_a, confirmed)
    mark_confirmed(admin_engine, confirmed)
    mark_reminded(admin_engine, clinic_a, silent)


def test_today_marks_confirmation_state_of_every_line(
        app_session_factory, admin_engine, clinic_a, doctor_named,
        service_cleaning):
    """Ради этого экран и делался: видно, кому звонить.

    Три состояния — подтвердил, молчит после напоминания, напоминание ещё
    не уходило — обязаны различаться в строке, иначе владелец обзванивает
    всех подряд, включая тех, кого ещё и не спрашивали."""
    three_states(admin_engine, clinic_a, doctor_named, service_cleaning)
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)

    send_admin(worker, app_session_factory, clinic_a, "/today")

    body = last_to(api, ADMIN_CHAT)
    assert line_for(body, "09:00").startswith("✅"), body
    assert line_for(body, "10:00").startswith("⏳"), body
    assert line_for(body, "11:00").startswith("11:00"), \
        f"запись без напоминания помечена как молчун: {body}"
    assert at("today_call_hint", "ru", count=1) in body, \
        f"владельцу не сказано, сколько человек не ответили: {body}"


def test_today_without_silent_patients_has_no_call_hint(
        app_session_factory, admin_engine, clinic_a, doctor_named,
        service_cleaning):
    """Все подтвердили — звонить некому: строка «стоит позвонить» в такой
    день только шумит, и ⏳ в списке взяться неоткуда."""
    first = seed_today(admin_engine, clinic_a, doctor_named, service_cleaning,
                       "09:00", chat_id=CHAT)
    second = seed_today(admin_engine, clinic_a, doctor_named, service_cleaning,
                        "10:00", chat_id=CHAT + 1)
    for appointment_id in (first, second):
        mark_reminded(admin_engine, clinic_a, appointment_id)
        mark_confirmed(admin_engine, appointment_id)
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)

    send_admin(worker, app_session_factory, clinic_a, "/today")

    body = last_to(api, ADMIN_CHAT)
    assert line_for(body, "09:00").startswith("✅"), body
    assert line_for(body, "10:00").startswith("✅"), body
    assert "⏳" not in body, body
    assert "⚠️" not in body, f"подсказка о звонках осталась в полном дне: {body}"


def test_morning_summary_carries_the_call_hint(
        app_session_factory, admin_engine, clinic_a, doctor_named,
        service_cleaning):
    """Сводка в 08:30 — единственный экран, который владелец видит ДО начала
    приёма: молчуны обязаны доехать в ней, а не только в /today по запросу."""
    three_states(admin_engine, clinic_a, doctor_named, service_cleaning)
    api = FakeTelegramAPI()
    service = morning_service(app_session_factory, clinic_a, api)
    moment = datetime.now(TASHKENT).replace(hour=8, minute=31)

    assert service.maybe_send_morning_summary(now_local=moment) is True

    for _, body, _ in api.sent:
        assert at("today_call_hint", "ru", count=1) in body, body
        assert line_for(body, "09:00").startswith("✅"), body
