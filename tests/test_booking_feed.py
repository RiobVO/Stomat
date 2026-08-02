"""Лента записей владельцу: карточка в админ-чаты в момент события.

Клиника без Google Calendar не видит записи бота вообще — только цифры
вечернего дайджеста. Лента закрывает дыру: подтверждение, отмена и перенос
приходят карточкой во ВСЕ админ-чаты сразу, на языке каждого чата.
Карточка — сигнал, а не канал ответа: якорь admin_relay на неё не пишется.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from conftest import at_tashkent, make_doctor, next_monday
from navbat.calendar.sync import CalendarSync
from navbat.db.base import tenant_transaction
from navbat.dialog.conversation import load_conversation, save_conversation
from navbat.dialog.escalation import booking_feed
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.patients import create_patient
from navbat.dialog.replies import service_label
from navbat.dialog.reschedule_flow import reslot_action
from navbat.nlu.extractor import FakeExtractor
from navbat.telegram.escalation import TelegramEscalation, build_escalation
from test_dialog_booking import CHAT, RecordingNotifier, explicit, extr, slot_buttons
from test_gcal_export import FakeCalendarAPI, bind_calendar, book
from test_tg_worker import FakeTelegramAPI

ADMIN_RU, ADMIN_SECOND, ADMIN_UZ = 8001, 8002, 8003
DOCTOR = "Акмаль Каримов"
PATIENT_NAME = "Алишер"


@pytest.fixture
def doctor_named(admin_engine, clinic_a):
    """Врач с именем: карточка обязана назвать того, к кому записали."""
    return make_doctor(admin_engine, clinic_a, name=DOCTOR)


def make_engine(app_session_factory, clinic_id, api, admin_chats, script=()):
    """Диалог с НАСТОЯЩИМ телеграм-нотификатором: ленту шлёт он, фейковые
    нотификаторы соседних тестов её не видят."""
    return DialogEngine(app_session_factory, clinic_id,
                        extractor=FakeExtractor(script=list(script)),
                        notifier=build_escalation(api, app_session_factory,
                                                  clinic_id, admin_chats))


def cards(api, chat: int) -> list[str]:
    return [body for target, body, _ in api.sent if target == chat]


def add_patient(app_session_factory, clinic_id, name: str = PATIENT_NAME):
    with tenant_transaction(app_session_factory, clinic_id) as session:
        return create_patient(session, tg_chat_id=CHAT, name=name,
                              phone="901234567")


def set_created_at(admin_engine, appointment_id, moment) -> None:
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE appointment SET created_at = :at "
                          "WHERE id = :id"),
                     {"at": moment, "id": appointment_id})


def anchors(admin_engine) -> int:
    with admin_engine.begin() as conn:
        return conn.execute(text("SELECT count(*) FROM admin_relay")).scalar_one()


# ── Диалог: запись, отмена, перенос ─────────────────────────────────────────

def test_confirmed_booking_reaches_every_admin_chat(
        app_session_factory, admin_engine, clinic_a, doctor_named,
        service_cleaning):
    """Главная ценность фичи: владелец видит запись в момент подтверждения,
    со всем, что нужно для звонка, — имя, услуга, время, врач."""
    day = next_monday()
    api = FakeTelegramAPI()
    engine = make_engine(app_session_factory, clinic_a, api,
                         [ADMIN_RU, ADMIN_SECOND],
                         [extr(service="cleaning", date_ref=explicit(day))])

    offer = engine.handle_text(CHAT, "хочу чистку в понедельник")
    engine.handle_action(CHAT, slot_buttons(offer)[0].action)
    engine.handle_text(CHAT, PATIENT_NAME)
    engine.handle_contact(CHAT, "998901234567", own=True)

    assert [chat for chat, _, _ in api.sent] == [ADMIN_RU, ADMIN_SECOND], \
        "карточка уходит веером всем админ-чатам клиники"
    for body in cards(api, ADMIN_RU) + cards(api, ADMIN_SECOND):
        assert body.startswith("✅"), body
        # имя пациента привязывается к записи ОТДЕЛЬНОЙ транзакцией уже после
        # confirm — карточка обязана назвать пациента всё равно
        assert PATIENT_NAME in body, body
        assert service_label("cleaning", "ru") in body, body
        assert f"{day:%d.%m} 09:00" in body, body
        assert DOCTOR in body, body
    assert anchors(admin_engine) == 0, \
        "лента — не канал ответа: свайп-реплаить на карточку некому"


def test_cancel_from_reminder_sends_a_card(app_session_factory, clinic_a,
                                           doctor_named, service_cleaning):
    """Отмена кнопкой из напоминания: слот освободился — владелец узнаёт
    об этом сразу, а не вечером в дайджесте."""
    day = next_monday()
    patient_id = add_patient(app_session_factory, clinic_a)
    appointment_id, _ = book(app_session_factory, clinic_a, doctor_named,
                             service_cleaning, day, "10:00", chat_id=CHAT,
                             patient_id=patient_id)
    api = FakeTelegramAPI()
    engine = make_engine(app_session_factory, clinic_a, api, [ADMIN_RU])

    engine.handle_action(CHAT, f"remind_cancel:{appointment_id}")
    engine.handle_action(CHAT, "cancel_yes")

    assert len(cards(api, ADMIN_RU)) == 1, cards(api, ADMIN_RU)
    body = cards(api, ADMIN_RU)[0]
    assert body.startswith("❌"), body
    assert PATIENT_NAME in body and f"{day:%d.%m} 10:00" in body, body
    assert DOCTOR in body, body


def test_reschedule_sends_a_card_with_the_new_time(
        app_session_factory, clinic_a, doctor_named, service_cleaning):
    """Перенос — тоже событие владельца: в карточке НОВОЕ время."""
    day = next_monday()
    patient_id = add_patient(app_session_factory, clinic_a)
    appointment_id, _ = book(app_session_factory, clinic_a, doctor_named,
                             service_cleaning, day, "09:00", chat_id=CHAT,
                             patient_id=patient_id)
    api = FakeTelegramAPI()
    engine = make_engine(app_session_factory, clinic_a, api, [ADMIN_RU])

    engine.handle_action(CHAT, reslot_action(appointment_id,
                                             at_tashkent(day, "11:00")))

    assert len(cards(api, ADMIN_RU)) == 1, cards(api, ADMIN_RU)
    body = cards(api, ADMIN_RU)[0]
    assert body.startswith("🔄"), body
    assert f"{day:%d.%m} 11:00" in body, body
    assert "09:00" not in body, "перенос показывает новое время, не старое"


# ── Календарный синк: карточка ДОПОЛНЯЕТ эскалацию ──────────────────────────

def make_sync(app_session_factory, clinic_id, tg_api, admin_chats):
    cal = FakeCalendarAPI()
    sync = CalendarSync(app_session_factory, clinic_id, api=cal,
                        notifier=build_escalation(tg_api, app_session_factory,
                                                  clinic_id, admin_chats),
                        tg_api=tg_api)
    return sync, cal


def test_relocation_by_sync_adds_a_card_to_the_alert(
        app_session_factory, admin_engine, clinic_a, doctor_named,
        service_cleaning):
    """Ручное событие вытеснило запись, бот её перенёс: 🟡-эскалация
    объясняет ПОЧЕМУ, карточка ленты показывает ЧТО стало. Одно другого
    не заменяет."""
    bind_calendar(admin_engine, doctor_named)
    day = next_monday()
    book(app_session_factory, clinic_a, doctor_named, service_cleaning, day,
         "09:00", chat_id=CHAT)
    tg_api = FakeTelegramAPI()
    sync, cal = make_sync(app_session_factory, clinic_a, tg_api, [ADMIN_RU])
    sync.sync_doctor(doctor_named)

    cal.seed_manual_event(start=at_tashkent(day, "09:00"),
                          end=at_tashkent(day, "09:30"))
    sync.sync_doctor(doctor_named)

    to_admin = cards(tg_api, ADMIN_RU)
    assert any(body.startswith("🟡") for body in to_admin), to_admin
    card = [body for body in to_admin if body.startswith("🔄")]
    assert len(card) == 1, to_admin
    assert f"{day:%d.%m} 10:00" in card[0], card[0]
    # записи без пациента (запись с улицы, /forget) карточку не отменяют
    assert "без имени" in card[0], card[0]


def test_unrelocatable_cancel_by_sync_adds_a_card(
        app_session_factory, admin_engine, clinic_a, service_cleaning):
    """Переносить некуда — запись отменена: владелец обязан увидеть отмену
    карточкой, а не только «к сведению»."""
    doctor = make_doctor(admin_engine, clinic_a,
                         intervals={"mon": [["09:00", "09:30"]]}, name=DOCTOR)
    bind_calendar(admin_engine, doctor)
    day = next_monday()
    tg_api = FakeTelegramAPI()
    sync, cal = make_sync(app_session_factory, clinic_a, tg_api, [ADMIN_RU])

    # будущие понедельники заняты заранее: горизонт переноса — 14 дней
    cal.seed_manual_event(day=day + timedelta(days=7))
    cal.seed_manual_event(day=day + timedelta(days=14))
    sync.sync_doctor(doctor)

    book(app_session_factory, clinic_a, doctor, service_cleaning, day, "09:00",
         chat_id=CHAT)
    cal.seed_manual_event(start=at_tashkent(day, "09:00"),
                          end=at_tashkent(day, "09:30"))
    sync.sync_doctor(doctor)

    to_admin = cards(tg_api, ADMIN_RU)
    assert any(body.startswith("🟡") for body in to_admin), to_admin
    assert [body for body in to_admin if body.startswith("❌")], to_admin


# ── Ночная пометка, фолбэк и язык ───────────────────────────────────────────

def test_after_hours_booking_carries_the_moon(
        app_session_factory, admin_engine, clinic_a, doctor_named,
        service_cleaning):
    """«Бот записал, пока клиника спала» — главный аргумент продажи.
    Признак тот же, что у after_hours_booked в /stats: момент СОЗДАНИЯ
    записи вне рабочего окна дня."""
    day = next_monday()
    appointment_id, _ = book(app_session_factory, clinic_a, doctor_named,
                             service_cleaning, day, "10:00", chat_id=CHAT)
    api = FakeTelegramAPI()
    notifier = TelegramEscalation(api, admin_chat_id=[ADMIN_RU])

    set_created_at(admin_engine, appointment_id, at_tashkent(day, "03:00"))
    with tenant_transaction(app_session_factory, clinic_a) as session:
        booking_feed(notifier, session, appointment_id, "booked")
    assert "🌙" in api.sent[-1][1], api.sent[-1][1]

    set_created_at(admin_engine, appointment_id, at_tashkent(day, "11:00"))
    with tenant_transaction(app_session_factory, clinic_a) as session:
        booking_feed(notifier, session, appointment_id, "booked")
    assert "🌙" not in api.sent[-1][1], \
        "дневная запись ночной пометки не получает"


def test_notifier_without_the_channel_stays_silent(
        app_session_factory, clinic_a, doctor_named, service_cleaning):
    """Фейки тестов, LoggingEscalation и CLI канала ленты не имеют.
    Деградация — тишина, а не эскалация: карточка не повод звать человека."""
    appointment_id, _ = book(app_session_factory, clinic_a, doctor_named,
                             service_cleaning, next_monday(), "09:00",
                             chat_id=CHAT)
    notifier = RecordingNotifier()

    with tenant_transaction(app_session_factory, clinic_a) as session:
        booking_feed(notifier, session, appointment_id, "booked")

    assert notifier.calls == [], "лента не должна деградировать в notify()"


def test_card_speaks_the_language_of_each_admin_chat(
        app_session_factory, clinic_a, doctor_named, service_cleaning):
    """Разные админ-чаты одной клиники держат разные языки консоли —
    решить можно только при отправке (карта, №16)."""
    day = next_monday()
    with tenant_transaction(app_session_factory, clinic_a) as session:
        conv = load_conversation(session, ADMIN_UZ)
        conv.context.extras["adm_lang"] = "uz"
        save_conversation(session, conv)
    appointment_id, _ = book(app_session_factory, clinic_a, doctor_named,
                             service_cleaning, day, "09:00", chat_id=CHAT)
    api = FakeTelegramAPI()
    notifier = build_escalation(api, app_session_factory, clinic_a,
                                [ADMIN_RU, ADMIN_UZ])

    with tenant_transaction(app_session_factory, clinic_a) as session:
        booking_feed(notifier, session, appointment_id, "booked")

    sent = {chat: body for chat, body, _ in api.sent}
    assert service_label("cleaning", "ru") in sent[ADMIN_RU], sent[ADMIN_RU]
    assert service_label("cleaning", "uz") in sent[ADMIN_UZ], sent[ADMIN_UZ]
    assert sent[ADMIN_RU] != sent[ADMIN_UZ], "узбекский чат получил русскую карточку"
