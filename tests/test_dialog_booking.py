"""Поток записи: slot-filling -> слоты -> hold -> имя/телефон -> booked.

Экстрактор scripted (детерминированные слоты), даты — explicit_DD.MM на
будущий понедельник: тесты не зависят от дня запуска.
"""
from __future__ import annotations

from datetime import date, datetime, time as dt_time
from zoneinfo import ZoneInfo

from sqlalchemy import text

from conftest import at_tashkent, make_service, next_monday, next_sunday
from navbat.db.base import tenant_transaction
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.patients import create_patient
from navbat.dialog.replies import MEDICAL_DISCLAIMER
from navbat.nlu.extractor import ExtractionError, FakeExtractor
from navbat.nlu.schema import Extraction
from navbat.scheduling.engine import SchedulingEngine

TASHKENT = ZoneInfo("Asia/Tashkent")
CHAT = 100


class RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []
        self.contexts: list[dict] = []

    def notify(self, chat_id: int, reason: str, context: dict) -> None:
        self.calls.append((chat_id, reason))
        self.contexts.append(dict(context))


def extr(intent="book", service=None, doctor=None, date_ref=None,
         time_ref=None, language="ru", is_medical=False) -> Extraction:
    return Extraction(intent=intent, service=service, doctor=doctor,
                      date_ref=date_ref, time_ref=time_ref,
                      language=language, is_medical=is_medical)


def explicit(day: date) -> str:
    return f"explicit_{day:%d.%m}"


def make_engine(app_session_factory, clinic_id, script) -> DialogEngine:
    return DialogEngine(app_session_factory, clinic_id,
                        extractor=FakeExtractor(script=script),
                        notifier=RecordingNotifier())


def slot_buttons(reply):
    return [b for b in reply.buttons if b.action.startswith("slot:")]


def slot_start(button) -> datetime:
    return datetime.fromisoformat(button.action.split(":", 2)[2])


def fsm_state(admin_engine, chat_id=CHAT) -> str:
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT fsm_state FROM conversation WHERE tg_chat_id = :c"),
            {"c": chat_id},
        ).scalar_one()


def appt_status(admin_engine) -> str:
    with admin_engine.begin() as conn:
        return conn.execute(text("SELECT status FROM appointment")).scalar_one()


# ── Эскалация не должна выносить имя пациента персоналу (m1) ──────────────────

def test_escalation_context_omits_patient_name(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    # имя лежит в context['pending_name'] между шагом имени и подтверждением;
    # при эскалации (просьба человека на шаге телефона) весь context уходит
    # админу — имя пациента не должно утечь персоналу
    day = next_monday()
    notifier = RecordingNotifier()
    engine = DialogEngine(app_session_factory, clinic_a,
                          extractor=FakeExtractor(script=[extr(
                              service="cleaning", date_ref=explicit(day))]),
                          notifier=notifier)
    offer = engine.handle_text(CHAT, "чистка в понедельник")
    engine.handle_action(CHAT, slot_buttons(offer)[0].action)
    engine.handle_text(CHAT, "Гульнора")  # → context['pending_name']
    engine.handle_text(CHAT, "позовите администратора")  # → эскалация

    assert any("администратора" in reason for _, reason in notifier.calls)
    assert all("Гульнора" not in str(ctx) for ctx in notifier.contexts), \
        "имя пациента не должно попадать в контекст эскалации"


# ── Точное некруглое время: предпочтение, не жёсткий фильтр (M1) ──────────────

def test_exact_offgrid_time_offers_nearest_not_escalation(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    # пациент просит 14:15; сетка слотов 30-минутная (14:00, 14:30…) —
    # точного совпадения нет. Раньше: «нет слотов» + эскалация. Должно:
    # показать слоты дня, ближайший к 14:15 первым.
    day = next_monday()
    notifier = RecordingNotifier()
    engine = DialogEngine(app_session_factory, clinic_a,
                          extractor=FakeExtractor(script=[extr(
                              service="cleaning", date_ref=explicit(day),
                              time_ref="14:15")]),
                          notifier=notifier)

    reply = engine.handle_text(CHAT, "чистка в понедельник на 14:15")
    buttons = slot_buttons(reply)
    assert buttons, "некруглое время не должно давать «нет слотов»"
    assert notifier.calls == [], "ложной эскалации быть не должно"

    times = [slot_start(b).astimezone(TASHKENT).time() for b in buttons]
    target = 14 * 60 + 15
    nearest = min(times, key=lambda t: abs(t.hour * 60 + t.minute - target))
    assert times[0] == nearest, "первым — ближайший к запрошенному времени слот"


# ── Happy path ───────────────────────────────────────────────────────────────

def test_happy_path_new_patient(app_session_factory, admin_engine, clinic_a,
                                doctor_a, service_cleaning):
    day = next_monday()
    engine = make_engine(app_session_factory, clinic_a, [
        extr(service="cleaning", date_ref=explicit(day)),
    ])

    offer = engine.handle_text(CHAT, "хочу чистку в понедельник")
    slots = slot_buttons(offer)
    assert slots, "ожидались кнопки слотов"
    assert slot_start(slots[0]) == at_tashkent(day, "09:00")

    ask_name = engine.handle_action(CHAT, slots[0].action)
    assert not ask_name.buttons
    assert fsm_state(admin_engine) == "awaiting_name"

    ask_phone = engine.handle_text(CHAT, "Алишер")
    assert not ask_phone.buttons
    assert fsm_state(admin_engine) == "awaiting_phone"

    done = engine.handle_contact(CHAT, "998901234567", own=True)
    assert appt_status(admin_engine) == "booked"
    assert fsm_state(admin_engine) == "idle"
    assert "09:00" in done.text

    # пациент создан и привязан к записи
    with admin_engine.begin() as conn:
        row = conn.execute(
            text("SELECT p.id FROM appointment a JOIN patient p ON p.id = a.patient_id")
        ).one_or_none()
    assert row is not None


def test_known_patient_books_without_questions(app_session_factory, admin_engine,
                                               clinic_a, doctor_a, service_cleaning):
    with tenant_transaction(app_session_factory, clinic_a) as session:
        create_patient(session, tg_chat_id=CHAT, name="Алишер", phone="901234567")

    day = next_monday()
    engine = make_engine(app_session_factory, clinic_a,
                         [extr(service="cleaning", date_ref=explicit(day))])
    offer = engine.handle_text(CHAT, "запишите на чистку")
    done = engine.handle_action(CHAT, slot_buttons(offer)[0].action)

    assert appt_status(admin_engine) == "booked"
    assert fsm_state(admin_engine) == "idle"
    assert "09:00" in done.text


# ── Slot-filling ─────────────────────────────────────────────────────────────

def test_missing_service_asks_with_buttons(app_session_factory, admin_engine,
                                           clinic_a, doctor_a, service_cleaning):
    day = next_monday()
    engine = make_engine(app_session_factory, clinic_a,
                         [extr(service=None, date_ref=explicit(day))])
    reply = engine.handle_text(CHAT, "запишите меня на понедельник")
    actions = [b.action for b in reply.buttons]
    assert "service:cleaning" in actions
    assert fsm_state(admin_engine) == "booking_collect"

    offer = engine.handle_action(CHAT, "service:cleaning")
    assert slot_buttons(offer)


def test_missing_date_asks_with_buttons(app_session_factory, admin_engine,
                                        clinic_a, doctor_a, service_cleaning):
    engine = make_engine(app_session_factory, clinic_a,
                         [extr(service="cleaning")])
    reply = engine.handle_text(CHAT, "хочу чистку")
    assert any(b.action.startswith("date:") for b in reply.buttons)
    assert fsm_state(admin_engine) == "booking_collect"

    offer = engine.handle_action(CHAT, f"date:{next_monday().isoformat()}")
    assert slot_buttons(offer)


def test_symptom_without_service_skips_service_question(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    make_service(admin_engine, clinic_a, "checkup", 30)
    day = next_monday()
    engine = make_engine(app_session_factory, clinic_a, [
        extr(service="checkup", date_ref=explicit(day), is_medical=True, language="uz"),
    ])
    reply = engine.handle_text(CHAT, "tishim ogriyapti ertaga vaqt bormi")
    assert slot_buttons(reply), "симптом -> checkup, без вопроса об услуге"
    assert MEDICAL_DISCLAIMER["uz"] in reply.text


# ── Фильтры и подбор дня ─────────────────────────────────────────────────────

def test_time_ref_filters_slots(app_session_factory, clinic_a, doctor_a,
                                service_cleaning):
    day = next_monday()
    engine = make_engine(app_session_factory, clinic_a, [
        extr(service="cleaning", date_ref=explicit(day), time_ref="morning"),
    ])
    reply = engine.handle_text(CHAT, "чистку в понедельник утром")
    slots = slot_buttons(reply)
    assert slots
    for b in slots:
        assert slot_start(b).astimezone(TASHKENT).hour < 12


def test_no_slots_offers_nearest_day(app_session_factory, clinic_a, doctor_a,
                                     service_cleaning):
    sunday = next_sunday()  # врач не работает по воскресеньям
    engine = make_engine(app_session_factory, clinic_a, [
        extr(service="cleaning", date_ref=explicit(sunday)),
    ])
    reply = engine.handle_text(CHAT, "чистку в воскресенье")
    slots = slot_buttons(reply)
    assert slots
    # ближайший рабочий день после воскресенья — понедельник
    monday = sunday.toordinal() + 1
    for b in slots:
        assert slot_start(b).astimezone(TASHKENT).date().toordinal() == monday


def test_named_doctor_filters_slots(app_session_factory, admin_engine, clinic_a,
                                    doctor_a, service_cleaning):
    from conftest import make_doctor
    akmal = make_doctor(admin_engine, clinic_a, name="Akmal aka")
    day = next_monday()
    engine = make_engine(app_session_factory, clinic_a, [
        extr(service="cleaning", date_ref=explicit(day), doctor="Akmal"),
    ])
    reply = engine.handle_text(CHAT, "к Akmal aka на чистку в понедельник")
    slots = slot_buttons(reply)
    assert slots
    for b in slots:
        assert b.action.split(":", 2)[1] == str(akmal)


# ── Срывы брони ──────────────────────────────────────────────────────────────

def test_hold_expired_during_collection_reoffers(app_session_factory, admin_engine,
                                                 clinic_a, doctor_a, service_cleaning):
    day = next_monday()
    engine = make_engine(app_session_factory, clinic_a, [
        extr(service="cleaning", date_ref=explicit(day)),
    ])
    offer = engine.handle_text(CHAT, "чистку в понедельник")
    engine.handle_action(CHAT, slot_buttons(offer)[0].action)
    engine.handle_text(CHAT, "Алишер")

    # бронь протухла, пока пациент вспоминал номер
    with admin_engine.begin() as conn:
        conn.execute(text(
            "UPDATE appointment SET hold_expires_at = now() - interval '1 minute'"
        ))

    reply = engine.handle_contact(CHAT, "+998901234567", own=True)
    assert slot_buttons(reply), "после протухшей брони — свежие слоты"
    assert fsm_state(admin_engine) == "booking_offer_slots"


def test_slot_taken_reoffers_without_it(app_session_factory, admin_engine,
                                        clinic_a, doctor_a, service_cleaning):
    day = next_monday()
    engine = make_engine(app_session_factory, clinic_a, [
        extr(service="cleaning", date_ref=explicit(day)),
    ])
    offer = engine.handle_text(CHAT, "чистку в понедельник")
    target = slot_buttons(offer)[0]

    # конкурент успел занять слот напрямую через движок
    sched = SchedulingEngine(app_session_factory, clinic_a)
    rival = sched.hold(doctor_a, service_cleaning, slot_start(target),
                       tg_chat_id=999, source="bot")
    sched.confirm(rival)

    reply = engine.handle_action(CHAT, target.action)
    fresh = slot_buttons(reply)
    assert fresh, "после занятого слота — свежие варианты"
    assert all(slot_start(b) != slot_start(target) for b in fresh)


# ── Устаревшая slot-кнопка после сброса сценария не роняет бота (m3) ──────────

def test_stale_slot_button_without_service_reasks_gracefully(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    # m3: callback `slot:` из старого сообщения, нажатый когда сценарий уже
    # сброшен (service=None). Раньше ctx["service"] кидал KeyError; типизи-
    # рованный DialogContext (R2) отдаёт None → service_id None → hold даёт
    # InvalidSlotError, которое ловится → бот переспрашивает услугу, не падает.
    day = next_monday()
    engine = make_engine(app_session_factory, clinic_a, [
        extr(service="cleaning", date_ref=explicit(day)),
    ])
    offer = engine.handle_text(CHAT, "чистка в понедельник")
    stale = slot_buttons(offer)[0].action
    engine.handle_text(CHAT, "/start")  # сброс сценария: service очищен

    reply = engine.handle_action(CHAT, stale)  # не должно бросить

    assert any(b.action.startswith("service:") for b in reply.buttons), \
        "после сброса контекста stale-slot переспрашивает услугу"
    assert fsm_state(admin_engine) == "booking_collect"
    assert appt_count(admin_engine) == 0, "запись не создаётся без услуги"


def appt_count(admin_engine) -> int:
    with admin_engine.begin() as conn:
        return conn.execute(text("SELECT count(*) FROM appointment")).scalar_one()
