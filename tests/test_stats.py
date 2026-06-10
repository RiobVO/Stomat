"""Лайт-сводка админу: /stats по команде и вечерний дайджест."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from conftest import next_monday
from navbat.db.base import tenant_transaction
from navbat.nlu.wrappers import UsageRecorder
from navbat.reminders import ReminderService
from navbat.stats import collect_daily_stats, should_send_digest
from test_dialog_booking import CHAT, extr
from test_gcal_export import book
from test_tg_worker import FakeTelegramAPI, make_worker, put_message

TASHKENT = ZoneInfo("Asia/Tashkent")
ADMIN_CHAT = 777


def seed_activity(app_session_factory, admin_engine, clinic_a, doctor_a,
                  service_cleaning):
    """День клиники: 2 записи, 1 отмена, 1 напоминание, 1 LLM-вызов."""
    day = next_monday()
    book(app_session_factory, clinic_a, doctor_a, service_cleaning, day, "09:00",
         chat_id=CHAT)
    appointment_id, sched = book(app_session_factory, clinic_a, doctor_a,
                                 service_cleaning, day, "11:00", chat_id=CHAT + 1)
    sched.cancel(appointment_id)
    with admin_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO reminder (clinic_id, appointment_id, kind, send_at, "
            "status, sent_at) SELECT clinic_id, id, '120m', now(), 'sent', now() "
            "FROM appointment LIMIT 1"))
    UsageRecorder(app_session_factory, clinic_a, daily_cap=10**9).record(120, 30)


def test_collect_daily_stats_counts(app_session_factory, admin_engine, clinic_a,
                                    doctor_a, service_cleaning):
    seed_activity(app_session_factory, admin_engine, clinic_a, doctor_a,
                  service_cleaning)
    with tenant_transaction(app_session_factory, clinic_a) as session:
        stats = collect_daily_stats(session, datetime.now(TASHKENT).date(), TASHKENT)
    assert stats.booked == 2
    assert stats.cancelled == 1
    assert stats.reminders_sent == 1
    assert stats.llm_requests == 1
    assert stats.llm_tokens == 150


def test_stats_include_nlu_drift(app_session_factory, clinic_a):
    from navbat.stats import render_stats

    recorder = UsageRecorder(app_session_factory, clinic_a, daily_cap=10**9)
    recorder.record(120, 30)
    recorder.record_failure()
    recorder.record_repair()

    today = datetime.now(TASHKENT).date()
    with tenant_transaction(app_session_factory, clinic_a) as session:
        stats = collect_daily_stats(session, today, TASHKENT)
    assert (stats.nlu_failures, stats.nlu_repairs) == (1, 1)
    out = render_stats(stats, today)
    assert "сбоев: 1" in out and "repair: 1" in out


# ── C-3: p95 ответа за день ──────────────────────────────────────────────────

def test_p95_response_from_done_queue(app_session_factory, admin_engine, clinic_a):
    from navbat.stats import collect_daily_stats

    with admin_engine.begin() as conn:
        for upd, secs in ((1, 1), (2, 10)):
            conn.execute(text(
                "INSERT INTO message_queue (clinic_id, update_id, tg_chat_id, "
                "payload, status, created_at, completed_at) VALUES "
                "(:c, :u, 100, '{}', 'done', now() - make_interval(secs => :s), "
                "now())"), {"c": clinic_a, "u": upd, "s": secs})
    with tenant_transaction(app_session_factory, clinic_a) as session:
        stats = collect_daily_stats(session, datetime.now(TASHKENT).date(),
                                    TASHKENT)
    assert stats.p95_response_sec is not None
    assert 9.0 < stats.p95_response_sec < 10.0  # percentile_cont([1,10], 0.95)


def test_p95_rendered_in_stats():
    from navbat.stats import DailyStats, render_stats

    stats = DailyStats(booked=1, cancelled=0, escalated=0, reminders_sent=0,
                       llm_requests=0, llm_tokens=0, nlu_failures=0,
                       nlu_repairs=0, prevented_noshows=0, saved_revenue=0,
                       p95_response_sec=2.3)
    assert "p95" in render_stats(stats, date(2026, 6, 10))


# ── should_send_digest: чистые границы ───────────────────────────────────────

def test_digest_only_after_evening_hour():
    today = date(2026, 6, 8)
    early = datetime(2026, 6, 8, 20, 59, tzinfo=TASHKENT)
    late = datetime(2026, 6, 8, 21, 0, tzinfo=TASHKENT)
    assert should_send_digest(early, None) is False
    assert should_send_digest(late, None) is True
    assert should_send_digest(late, today) is False, "сегодня уже слали"
    assert should_send_digest(late, today - timedelta(days=1)) is True


# ── /stats в админ-чате ──────────────────────────────────────────────────────

def test_stats_command_from_admin_chat(app_session_factory, admin_engine, clinic_a,
                                       doctor_a, service_cleaning):
    seed_activity(app_session_factory, admin_engine, clinic_a, doctor_a,
                  service_cleaning)
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    put_message(app_session_factory, clinic_a, "/stats", chat_id=ADMIN_CHAT)
    worker.process_one()

    chat_id, message, _ = api.sent[0]
    assert chat_id == ADMIN_CHAT
    assert "2" in message and "1" in message, "цифры дня в сводке"


def test_stats_from_patient_goes_to_nlu(app_session_factory, admin_engine, clinic_a,
                                        doctor_a, service_cleaning):
    # пациент написал «/stats» — это обычный текст, не админ-команда
    worker, api, _ = make_worker(app_session_factory, clinic_a,
                                 [extr(intent="other")], admin_chat_id=ADMIN_CHAT)
    put_message(app_session_factory, clinic_a, "/stats", chat_id=CHAT)
    worker.process_one()
    assert api.sent[0][0] == CHAT


# ── Вечерний дайджест ────────────────────────────────────────────────────────

def test_evening_digest_sent_once(app_session_factory, admin_engine, clinic_a,
                                  doctor_a, service_cleaning):
    seed_activity(app_session_factory, admin_engine, clinic_a, doctor_a,
                  service_cleaning)
    api = FakeTelegramAPI()
    service = ReminderService(app_session_factory, clinic_a, tg_api=api,
                              digest_chat_id=ADMIN_CHAT)
    evening = datetime.now(TASHKENT).replace(hour=21, minute=30)

    assert service.maybe_send_digest(now_local=evening) is True
    assert api.sent[-1][0] == ADMIN_CHAT
    assert service.maybe_send_digest(now_local=evening) is False, "раз в день"

    with admin_engine.begin() as conn:
        last = conn.execute(text("SELECT last_digest_date FROM clinic")).scalar_one()
    assert last == evening.date()
