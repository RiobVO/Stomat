"""Жизненный цикл записи: confirm/cancel/reschedule, идемпотентность, аудит."""
import pytest
from sqlalchemy import text

from navbat.scheduling.errors import (
    DuplicateMessageError,
    HoldExpiredError,
    InvalidSlotError,
    SlotTakenError,
)

from conftest import at_tashkent, next_monday


def test_confirm_expired_hold_fails(sched, admin_engine, doctor_a, service_cleaning):
    appt_id = sched.hold(doctor_a, service_cleaning, at_tashkent(next_monday(), "09:00"))
    with admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE appointment SET hold_expires_at = now() - interval '1 second' "
                 "WHERE id = :id"),
            {"id": appt_id},
        )
    with pytest.raises(HoldExpiredError):
        sched.confirm(appt_id)


def test_cancel_frees_slot(sched, doctor_a, service_cleaning):
    day = next_monday()
    start = at_tashkent(day, "09:00")
    appt_id = sched.hold(doctor_a, service_cleaning, start)
    sched.confirm(appt_id)
    sched.cancel(appt_id)

    assert start in {s.start for s in sched.find_free_slots(doctor_a, service_cleaning, day)}
    sched.confirm(sched.hold(doctor_a, service_cleaning, start))  # слот реально свободен


def test_reschedule_moves_booking(sched, doctor_a, service_cleaning):
    day = next_monday()
    appt_id = sched.hold(doctor_a, service_cleaning, at_tashkent(day, "09:00"))
    sched.confirm(appt_id)

    sched.reschedule(appt_id, at_tashkent(day, "11:00"))

    starts = {s.start for s in sched.find_free_slots(doctor_a, service_cleaning, day)}
    assert at_tashkent(day, "09:00") in starts      # старый слот освободился
    assert at_tashkent(day, "11:00") not in starts  # новый занят


def test_reschedule_into_taken_slot_keeps_original(sched, doctor_a, service_cleaning):
    day = next_monday()
    first = sched.hold(doctor_a, service_cleaning, at_tashkent(day, "09:00"))
    sched.confirm(first)
    second = sched.hold(doctor_a, service_cleaning, at_tashkent(day, "10:00"))
    sched.confirm(second)

    with pytest.raises(SlotTakenError):
        sched.reschedule(second, at_tashkent(day, "09:00"))

    # вторая запись не потеряна и осталась на своём месте
    starts = {s.start for s in sched.find_free_slots(doctor_a, service_cleaning, day)}
    assert at_tashkent(day, "10:00") not in starts


def test_hold_outside_working_hours_rejected(sched, doctor_a, service_cleaning):
    with pytest.raises(InvalidSlotError):
        sched.hold(doctor_a, service_cleaning, at_tashkent(next_monday(), "03:00"))


def test_duplicate_telegram_message_rejected(sched, doctor_a, service_cleaning):
    day = next_monday()
    sched.hold(doctor_a, service_cleaning, at_tashkent(day, "09:00"),
               tg_chat_id=111, tg_message_id=5)
    with pytest.raises(DuplicateMessageError):
        sched.hold(doctor_a, service_cleaning, at_tashkent(day, "10:00"),
                   tg_chat_id=111, tg_message_id=5)  # тот же дубль, другой слот


def test_actions_are_audited(sched, admin_engine, doctor_a, service_cleaning):
    day = next_monday()
    appt_id = sched.hold(doctor_a, service_cleaning, at_tashkent(day, "09:00"))
    sched.confirm(appt_id)
    sched.cancel(appt_id)

    with admin_engine.connect() as conn:
        actions = conn.execute(
            text("SELECT action FROM appointment_audit WHERE appointment_id = :id "
                 "ORDER BY id"),
            {"id": appt_id},
        ).scalars().all()
    assert actions == ["hold", "confirm", "cancel"]
