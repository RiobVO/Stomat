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
        start = datetime.fromisoformat(b.action.split(":", 1)[1])
        assert start.astimezone(TASHKENT).date() == tuesday


def test_reschedule_without_appointment(app_session_factory, admin_engine, clinic_a,
                                        doctor_a, service_cleaning):
    engine = DialogEngine(app_session_factory, clinic_a,
                          extractor=FakeExtractor(script=[extr(intent="reschedule")]))
    reply = engine.handle_text(CHAT, "перенесите запись")
    assert not reply.buttons
    assert fsm_state(admin_engine) == "idle"
