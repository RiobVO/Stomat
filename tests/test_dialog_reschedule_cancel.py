"""Полные потоки reschedule и cancel поверх scheduling engine."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from conftest import at_tashkent, next_monday
from navbat.dialog.fsm import DialogEngine
from navbat.nlu.extractor import FakeExtractor
from navbat.scheduling.engine import SchedulingEngine
from test_dialog_booking import CHAT, explicit, extr, fsm_state

TASHKENT = ZoneInfo("Asia/Tashkent")


def book_directly(app_session_factory, clinic_id, doctor_id, service_id, day, hhmm):
    sched = SchedulingEngine(app_session_factory, clinic_id)
    appt = sched.hold(doctor_id, service_id, at_tashkent(day, hhmm), tg_chat_id=CHAT)
    sched.confirm(appt)
    return appt


def appt_row(admin_engine):
    with admin_engine.begin() as conn:
        return conn.execute(text(
            "SELECT status, lower(time_range) AS start FROM appointment"
        )).one()


# ── cancel ───────────────────────────────────────────────────────────────────

def test_cancel_with_confirmation(app_session_factory, admin_engine, clinic_a,
                                  doctor_a, service_cleaning):
    book_directly(app_session_factory, clinic_a, doctor_a, service_cleaning,
                  next_monday(), "09:00")
    engine = DialogEngine(app_session_factory, clinic_a,
                          extractor=FakeExtractor(script=[extr(intent="cancel")]))

    confirm = engine.handle_text(CHAT, "отмените мою запись")
    actions = [b.action for b in confirm.buttons]
    assert "cancel_yes" in actions and "cancel_no" in actions
    assert fsm_state(admin_engine) == "cancel_confirm"

    engine.handle_action(CHAT, "cancel_yes")
    assert appt_row(admin_engine).status == "cancelled"
    assert fsm_state(admin_engine) == "idle"


def test_cancel_declined_keeps_appointment(app_session_factory, admin_engine, clinic_a,
                                           doctor_a, service_cleaning):
    book_directly(app_session_factory, clinic_a, doctor_a, service_cleaning,
                  next_monday(), "09:00")
    engine = DialogEngine(app_session_factory, clinic_a,
                          extractor=FakeExtractor(script=[extr(intent="cancel")]))
    engine.handle_text(CHAT, "отмените запись")
    engine.handle_action(CHAT, "cancel_no")

    assert appt_row(admin_engine).status == "booked"
    assert fsm_state(admin_engine) == "idle"


def test_cancel_without_appointment(app_session_factory, admin_engine, clinic_a,
                                    doctor_a, service_cleaning):
    engine = DialogEngine(app_session_factory, clinic_a,
                          extractor=FakeExtractor(script=[extr(intent="cancel")]))
    reply = engine.handle_text(CHAT, "отмените запись")
    assert not reply.buttons
    assert fsm_state(admin_engine) == "idle"


# ── reschedule ───────────────────────────────────────────────────────────────

def test_reschedule_to_new_day(app_session_factory, admin_engine, clinic_a,
                               doctor_a, service_cleaning):
    monday = next_monday()
    tuesday = monday + timedelta(days=1)
    book_directly(app_session_factory, clinic_a, doctor_a, service_cleaning,
                  monday, "09:00")
    engine = DialogEngine(app_session_factory, clinic_a, extractor=FakeExtractor(
        script=[extr(intent="reschedule", date_ref=explicit(tuesday))]))

    offer = engine.handle_text(CHAT, "перенесите на вторник")
    reslots = [b for b in offer.buttons if b.action.startswith("reslot:")]
    assert reslots
    assert fsm_state(admin_engine) == "resched_offer_slots"

    engine.handle_action(CHAT, reslots[0].action)
    row = appt_row(admin_engine)
    assert row.status == "booked"
    assert row.start.astimezone(TASHKENT).date() == tuesday
    assert fsm_state(admin_engine) == "idle"


def test_reschedule_without_date_asks(app_session_factory, admin_engine, clinic_a,
                                      doctor_a, service_cleaning):
    monday = next_monday()
    book_directly(app_session_factory, clinic_a, doctor_a, service_cleaning,
                  monday, "09:00")
    engine = DialogEngine(app_session_factory, clinic_a,
                          extractor=FakeExtractor(script=[extr(intent="reschedule")]))

    ask = engine.handle_text(CHAT, "хочу перенести запись")
    assert any(b.action.startswith("date:") for b in ask.buttons)

    tuesday = monday + timedelta(days=1)
    offer = engine.handle_action(CHAT, f"date:{tuesday.isoformat()}")
    reslots = [b for b in offer.buttons if b.action.startswith("reslot:")]
    assert reslots
    for b in reslots:
        # reslot:<id записи>:<минуты эпохи> — кнопка несёт и запись, и время
        minutes = int(b.action.rsplit(":", 1)[1])
        start = datetime.fromtimestamp(minutes * 60, tz=TASHKENT)
        assert start.date() == tuesday


def test_reschedule_without_appointment(app_session_factory, admin_engine, clinic_a,
                                        doctor_a, service_cleaning):
    engine = DialogEngine(app_session_factory, clinic_a,
                          extractor=FakeExtractor(script=[extr(intent="reschedule")]))
    reply = engine.handle_text(CHAT, "перенесите запись")
    assert not reply.buttons
    assert fsm_state(admin_engine) == "idle"


def test_stale_reslot_without_pending_reschedule_does_not_crash(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    """Кнопка альтернативы висит в чате до самого приёма и приходит сырой.

    К моменту тапа сценарий переноса мог закрыться (/start, другой поток,
    retention снёс диалог) — переносить нечего, а id записи в кнопке нет.
    Без снимка это падало приведением типа и уводило тап в dead letter."""
    engine = DialogEngine(app_session_factory, clinic_a,
                          extractor=FakeExtractor(script=[]))
    start = at_tashkent(next_monday(), "11:00").isoformat()

    reply = engine.handle_action(CHAT, f"reslot:{start}")
    assert reply.text and fsm_state(admin_engine) == "idle"


def test_reslot_with_garbage_time_does_not_crash(app_session_factory, admin_engine,
                                                 clinic_a, doctor_a,
                                                 service_cleaning):
    """Время приходит из callback_data, то есть от клиента: сырой префикс
    больше не прикрыт нумерацией, и мусор обязан гаситься, а не падать."""
    book_directly(app_session_factory, clinic_a, doctor_a, service_cleaning,
                  next_monday(), "09:00")
    engine = DialogEngine(app_session_factory, clinic_a, extractor=FakeExtractor(
        script=[extr(intent="reschedule",
                     date_ref=explicit(next_monday() + timedelta(days=1)))]))
    engine.handle_text(CHAT, "перенесите на вторник")

    reply = engine.handle_action(CHAT, "reslot:не-время")
    assert reply.text
    assert appt_row(admin_engine).status == "booked"


def test_reslot_into_the_past_is_refused(app_session_factory, admin_engine,
                                         clinic_a, doctor_a, service_cleaning):
    """Время в сырой кнопке — данные от клиента, а движок сверяет старт
    только с рабочей сеткой врача: валидное «10:00 позапрошлого
    понедельника» на сетке лежит и уносило запись в прошлое."""
    monday = next_monday()
    appointment = book_directly(app_session_factory, clinic_a, doctor_a,
                                service_cleaning, monday, "09:00")
    engine = DialogEngine(app_session_factory, clinic_a,
                          extractor=FakeExtractor(script=[]))
    past = at_tashkent(monday - timedelta(days=14), "10:00")

    engine.handle_action(
        CHAT, f"reslot:{appointment}:{int(past.timestamp()) // 60}")

    assert appt_row(admin_engine).start == at_tashkent(monday, "09:00"), \
        "запись уехала в прошлое"


def test_reslot_does_not_move_another_patients_appointment(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    """id записи приходит из callback_data: чужой id внутри той же клиники
    переносил бы чужую запись (RLS изолирует клиники, но не пациентов)."""
    monday = next_monday()
    victim_chat = CHAT + 1
    sched = SchedulingEngine(app_session_factory, clinic_a)
    victim = sched.hold(doctor_a, service_cleaning, at_tashkent(monday, "11:00"),
                        tg_chat_id=victim_chat)
    sched.confirm(victim)
    engine = DialogEngine(app_session_factory, clinic_a,
                          extractor=FakeExtractor(script=[]))
    target = at_tashkent(monday, "14:00")

    engine.handle_action(
        CHAT, f"reslot:{victim}:{int(target.timestamp()) // 60}")

    with admin_engine.begin() as conn:
        start = conn.execute(text(
            "SELECT lower(time_range) FROM appointment WHERE id = :id"),
            {"id": victim}).scalar_one()
    assert start == at_tashkent(monday, "11:00"), "перенесена чужая запись"
