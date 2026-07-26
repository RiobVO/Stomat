"""Дашборд «язык денег»: предотвращённые неявки и сохранённая выручка (E.1).

Неявка «предотвращена» (BRIEF), когда пациент отменил ИЗ НАПОМИНАНИЯ и слот
потом перезаписан другой booked-записью; сохранённая выручка — цена услуг
новых записей. Отмена из меню в метрику не идёт (actor «bot»).
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text

from conftest import make_service, next_monday
from navbat.db.base import tenant_transaction
from navbat.dialog.fsm import DialogEngine
from navbat.nlu.extractor import FakeExtractor
from navbat.stats import collect_daily_stats, render_stats
from test_dialog_booking import CHAT, extr
from test_gcal_export import book

TASHKENT = ZoneInfo("Asia/Tashkent")


def cancel_from_reminder(app_session_factory, clinic_a, appointment_id) -> None:
    engine = DialogEngine(app_session_factory, clinic_a,
                          extractor=FakeExtractor(script=[]))
    engine.handle_action(CHAT, f"remind_cancel:{appointment_id}")
    engine.handle_action(CHAT, "cancel_yes")


def audit_cancel_actor(admin_engine) -> str:
    with admin_engine.begin() as conn:
        return conn.execute(text(
            "SELECT actor FROM appointment_audit WHERE action = 'cancel'"
        )).scalar_one()


def daily_stats(app_session_factory, clinic_a):
    with tenant_transaction(app_session_factory, clinic_a) as session:
        return collect_daily_stats(session, datetime.now(TASHKENT).date(),
                                   TASHKENT)


def test_reminder_cancel_with_resold_slot_counts_money(app_session_factory,
                                                       admin_engine, clinic_a,
                                                       doctor_a):
    service = make_service(admin_engine, clinic_a, "filling", 60, price=400_000)
    day = next_monday()
    appointment_id, _ = book(app_session_factory, clinic_a, doctor_a, service,
                             day, "09:00", chat_id=CHAT)
    cancel_from_reminder(app_session_factory, clinic_a, appointment_id)
    assert audit_cancel_actor(admin_engine) == "reminder"

    # слот перепродан другому пациенту
    book(app_session_factory, clinic_a, doctor_a, service, day, "09:00",
         chat_id=CHAT + 1)

    stats = daily_stats(app_session_factory, clinic_a)
    assert stats.prevented_noshows == 1
    assert stats.saved_revenue == 400_000
    out = render_stats(stats, datetime.now(TASHKENT).date())
    assert "предотвращено неявок: 1" in out
    assert "400 000" in out


def test_reminder_cancel_without_resale_not_counted(app_session_factory,
                                                    admin_engine, clinic_a,
                                                    doctor_a):
    service = make_service(admin_engine, clinic_a, "filling", 60, price=400_000)
    appointment_id, _ = book(app_session_factory, clinic_a, doctor_a, service,
                             next_monday(), "09:00", chat_id=CHAT)
    cancel_from_reminder(app_session_factory, clinic_a, appointment_id)

    stats = daily_stats(app_session_factory, clinic_a)
    assert (stats.prevented_noshows, stats.saved_revenue) == (0, 0), \
        "слот не перепродан — неявка не «предотвращена» (BRIEF-определение)"


def test_menu_cancel_with_resale_not_counted(app_session_factory, admin_engine,
                                             clinic_a, doctor_a):
    service = make_service(admin_engine, clinic_a, "filling", 60, price=400_000)
    day = next_monday()
    book(app_session_factory, clinic_a, doctor_a, service, day, "09:00",
         chat_id=CHAT)
    engine = DialogEngine(app_session_factory, clinic_a,
                          extractor=FakeExtractor(script=[extr(intent="cancel")]))
    engine.handle_text(CHAT, "отмените мою запись")
    engine.handle_action(CHAT, "cancel_yes")
    assert audit_cancel_actor(admin_engine) == "bot"

    book(app_session_factory, clinic_a, doctor_a, service, day, "09:00",
         chat_id=CHAT + 1)
    stats = daily_stats(app_session_factory, clinic_a)
    assert (stats.prevented_noshows, stats.saved_revenue) == (0, 0), \
        "обычная отмена — не история про напоминания"
