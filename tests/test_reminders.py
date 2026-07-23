"""Напоминания 24ч/2ч: reconciliation из БД (не таймеры в памяти), retry, кнопки.

Требование BRIEF: вычисляются запросом к appointment — переживают рестарт;
трекинг доставки; backoff → dead letter → алерт.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text

from conftest import at_tashkent, next_monday
from navbat.reminders import ReminderService
from test_dialog_booking import CHAT, RecordingNotifier
from test_gcal_export import book
from test_tg_worker import FakeTelegramAPI

# запись далеко в будущем: оба дефолтных офсета (24ч/2ч) гарантированно впереди
def far_monday():
    return next_monday() + timedelta(days=7)


def make_service_obj(app_session_factory, clinic_id, **kwargs):
    api = FakeTelegramAPI()
    notifier = RecordingNotifier()
    service = ReminderService(app_session_factory, clinic_id, tg_api=api,
                              notifier=notifier, **kwargs)
    return service, api, notifier


def reminder_rows(admin_engine):
    with admin_engine.begin() as conn:
        return conn.execute(text(
            "SELECT kind, status, send_at FROM reminder ORDER BY send_at"
        )).all()


def ripen_all(admin_engine):
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE reminder SET send_at = now() - interval '1 minute' "
                          "WHERE status = 'pending'"))


def test_reconcile_creates_reminders_per_offset(app_session_factory, admin_engine,
                                                clinic_a, doctor_a, service_cleaning):
    day = far_monday()
    book(app_session_factory, clinic_a, doctor_a, service_cleaning, day, "09:00",
         chat_id=CHAT)
    service, _, _ = make_service_obj(app_session_factory, clinic_a)
    service.reconcile()

    rows = reminder_rows(admin_engine)
    assert [r.status for r in rows] == ["pending", "pending"]
    starts = {r.kind: r.send_at for r in rows}
    appointment_start = at_tashkent(day, "09:00")
    assert starts["1440m"] == appointment_start - timedelta(hours=24)
    assert starts["120m"] == appointment_start - timedelta(hours=2)

    service.reconcile()  # идемпотентность
    assert len(reminder_rows(admin_engine)) == 2


def test_past_offset_is_not_created(app_session_factory, admin_engine, clinic_a,
                                    doctor_a, service_cleaning):
    book(app_session_factory, clinic_a, doctor_a, service_cleaning,
         far_monday(), "09:00", chat_id=CHAT)
    # офсет 60 дней — send_at в прошлом, такое напоминание бессмысленно
    service, _, _ = make_service_obj(
        app_session_factory, clinic_a,
        offsets=(timedelta(days=60), timedelta(hours=2)))
    service.reconcile()
    assert [r.kind for r in reminder_rows(admin_engine)] == ["120m"]


def test_reschedule_moves_send_at(app_session_factory, admin_engine, clinic_a,
                                  doctor_a, service_cleaning):
    day = far_monday()
    appointment_id, sched = book(app_session_factory, clinic_a, doctor_a,
                                 service_cleaning, day, "09:00", chat_id=CHAT)
    service, _, _ = make_service_obj(app_session_factory, clinic_a)
    service.reconcile()

    sched.reschedule(appointment_id, at_tashkent(day, "11:00"))
    service.reconcile()

    rows = reminder_rows(admin_engine)
    assert len(rows) == 2
    starts = {r.kind: r.send_at for r in rows}
    assert starts["120m"] == at_tashkent(day, "11:00") - timedelta(hours=2)


def test_cancelled_appointment_cancels_pending(app_session_factory, admin_engine,
                                               clinic_a, doctor_a, service_cleaning):
    appointment_id, sched = book(app_session_factory, clinic_a, doctor_a,
                                 service_cleaning, far_monday(), "09:00",
                                 chat_id=CHAT)
    service, _, _ = make_service_obj(app_session_factory, clinic_a)
    service.reconcile()

    sched.cancel(appointment_id)
    service.reconcile()
    assert all(r.status == "cancelled" for r in reminder_rows(admin_engine))


def test_send_due_sends_only_ripe_with_buttons(app_session_factory, admin_engine,
                                               clinic_a, doctor_a, service_cleaning):
    book(app_session_factory, clinic_a, doctor_a, service_cleaning,
         far_monday(), "09:00", chat_id=CHAT)
    service, api, _ = make_service_obj(app_session_factory, clinic_a)
    service.reconcile()

    assert service.send_due() == 0, "оба напоминания ещё не созрели"
    ripen_all(admin_engine)
    assert service.send_due() == 2

    chat_id, message_text, buttons = api.sent[0]
    assert chat_id == CHAT
    assert "09:00" in message_text
    assert len(buttons) == 2
    with admin_engine.begin() as conn:
        actions = conn.execute(text(
            "SELECT context -> 'tg_actions' FROM conversation WHERE tg_chat_id = :c"),
            {"c": CHAT}).scalar_one()
    assert actions["1"].startswith("attend:")
    assert actions["2"].startswith("remind_cancel:")

    assert service.send_due() == 0, "sent не переотправляются"
    statuses = {r.status for r in reminder_rows(admin_engine)}
    assert statuses == {"sent"}


def test_send_failures_go_to_dead_letter_with_alert(app_session_factory,
                                                    admin_engine, clinic_a,
                                                    doctor_a, service_cleaning):
    book(app_session_factory, clinic_a, doctor_a, service_cleaning,
         far_monday(), "09:00", chat_id=CHAT)
    service, api, notifier = make_service_obj(
        app_session_factory, clinic_a, offsets=(timedelta(hours=2),))
    service.reconcile()
    ripen_all(admin_engine)

    api.send_failures = 3
    for _ in range(3):
        service.send_due()

    assert [r.status for r in reminder_rows(admin_engine)] == ["failed"]
    assert notifier.calls, "dead letter напоминания — алерт админу"
    assert service.send_due() == 0
