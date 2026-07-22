"""Финальная перепроверка слота перед confirm (BRIEF): GCal мог уйти вперёд БД.

Guard опционален: без него FSM работает как раньше (все старые тесты).
"""
from __future__ import annotations

from sqlalchemy import text

from conftest import next_monday
from navbat.calendar.api import CalendarAPIError
from navbat.calendar.guard import CalendarSlotGuard
from navbat.db.base import tenant_transaction
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.patients import create_patient
from navbat.nlu.extractor import FakeExtractor
from test_dialog_booking import CHAT, explicit, extr, slot_buttons
from test_gcal_export import CAL, FakeCalendarAPI, bind_calendar


class StubGuard:
    def __init__(self, free: bool) -> None:
        self._free = free
        self.calls = 0

    def is_free(self, doctor_id, start, end) -> bool:
        self.calls += 1
        return self._free


def booked_engine(app_session_factory, clinic_a, guard):
    with tenant_transaction(app_session_factory, clinic_a) as session:
        create_patient(session, tg_chat_id=CHAT, name="Алишер", phone="901234567")
    return DialogEngine(
        app_session_factory, clinic_a,
        extractor=FakeExtractor(script=[
            extr(service="cleaning", date_ref=explicit(next_monday())),
        ]),
        slot_guard=guard,
    )


def statuses(admin_engine) -> list[str]:
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT status FROM appointment ORDER BY created_at")
        ).scalars().all()


def test_busy_in_gcal_rejects_confirm_and_reoffers(app_session_factory, admin_engine,
                                                   clinic_a, doctor_a, service_cleaning):
    guard = StubGuard(free=False)
    engine = booked_engine(app_session_factory, clinic_a, guard)
    offer = engine.handle_text(CHAT, "чистку в понедельник")
    reply = engine.handle_action(CHAT, slot_buttons(offer)[0].action)

    assert guard.calls == 1
    assert "booked" not in statuses(admin_engine), "подтверждение отклонено"
    assert slot_buttons(reply), "пациенту — свежие слоты, не тишина"


def test_free_in_gcal_confirms(app_session_factory, admin_engine, clinic_a,
                               doctor_a, service_cleaning):
    engine = booked_engine(app_session_factory, clinic_a, StubGuard(free=True))
    offer = engine.handle_text(CHAT, "чистку в понедельник")
    engine.handle_action(CHAT, slot_buttons(offer)[0].action)
    assert statuses(admin_engine) == ["booked"]


# ── CalendarSlotGuard поверх freeBusy ────────────────────────────────────────

class BusyFake(FakeCalendarAPI):
    def __init__(self, busy: bool = False, broken: bool = False) -> None:
        super().__init__()
        self.busy = busy
        self.broken = broken

    def free_busy(self, calendar_id, time_min, time_max) -> bool:
        if self.broken:
            raise CalendarAPIError("Google лёг")
        return self.busy


def test_calendar_guard_checks_free_busy(app_session_factory, admin_engine,
                                         clinic_a, doctor_a):
    bind_calendar(admin_engine, doctor_a)
    from datetime import datetime, timezone
    moment = datetime.now(timezone.utc)

    busy_guard = CalendarSlotGuard(app_session_factory, clinic_a, BusyFake(busy=True))
    assert busy_guard.is_free(doctor_a, moment, moment) is False

    free_guard = CalendarSlotGuard(app_session_factory, clinic_a, BusyFake(busy=False))
    assert free_guard.is_free(doctor_a, moment, moment) is True


def test_calendar_guard_graceful_without_calendar_or_google(app_session_factory,
                                                            admin_engine, clinic_a,
                                                            doctor_a):
    from datetime import datetime, timezone
    moment = datetime.now(timezone.utc)

    # календарь не привязан — проверять нечего
    guard = CalendarSlotGuard(app_session_factory, clinic_a, BusyFake(busy=True))
    assert guard.is_free(doctor_a, moment, moment) is True

    # Google недоступен — не блокируем запись: БД-constraint остаётся гарантией
    bind_calendar(admin_engine, doctor_a)
    broken = CalendarSlotGuard(app_session_factory, clinic_a, BusyFake(broken=True))
    assert broken.is_free(doctor_a, moment, moment) is True
