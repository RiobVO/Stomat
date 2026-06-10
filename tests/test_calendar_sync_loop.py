"""Алерт при затяжном сбое синхронизации календаря (M5): N циклов подряд
со сбоем → эскалация админу, восстановление → уведомление."""
from __future__ import annotations

from sqlalchemy import text

from conftest import make_doctor
from navbat.calendar.sync_loop import FAILURE_ALERT_THRESHOLD, CalendarSyncLoop
from test_dialog_booking import RecordingNotifier

ADMIN = 7_000_001


class _Sync:
    def __init__(self, fail: bool = True) -> None:
        self.fail = fail
        self.calls = 0

    def sync_doctor(self, doctor_id) -> None:
        self.calls += 1
        if self.fail:
            raise RuntimeError("Google недоступен")


def _loop_with_calendar_doctor(app_session_factory, admin_engine, clinic_a, sync,
                               notifier):
    doctor_id = make_doctor(admin_engine, clinic_a)
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE doctor SET gcal_calendar_id = 'cal@x' "
                          "WHERE id = :d"), {"d": doctor_id})
    return CalendarSyncLoop(app_session_factory, clinic_a, sync, notifier, ADMIN)


def test_alerts_once_after_threshold(app_session_factory, admin_engine, clinic_a):
    sync, notifier = _Sync(fail=True), RecordingNotifier()
    loop = _loop_with_calendar_doctor(app_session_factory, admin_engine, clinic_a,
                                      sync, notifier)
    for _ in range(FAILURE_ALERT_THRESHOLD - 1):
        loop.run_once()
    assert notifier.calls == []  # ниже порога — молчим
    loop.run_once()  # порог достигнут
    assert len(notifier.calls) == 1
    chat_id, reason = notifier.calls[0]
    assert chat_id == ADMIN
    assert "Google" in reason
    loop.run_once()  # продолжает падать — НЕ спамим повторно
    assert len(notifier.calls) == 1


def test_recovery_resets_and_notifies(app_session_factory, admin_engine, clinic_a):
    sync, notifier = _Sync(fail=True), RecordingNotifier()
    loop = _loop_with_calendar_doctor(app_session_factory, admin_engine, clinic_a,
                                      sync, notifier)
    for _ in range(FAILURE_ALERT_THRESHOLD):
        loop.run_once()
    assert len(notifier.calls) == 1  # алерт о сбое
    sync.fail = False
    loop.run_once()  # восстановление
    assert len(notifier.calls) == 2
    assert "восстановлена" in notifier.calls[1][1]


def test_no_alert_when_healthy(app_session_factory, admin_engine, clinic_a):
    sync, notifier = _Sync(fail=False), RecordingNotifier()
    loop = _loop_with_calendar_doctor(app_session_factory, admin_engine, clinic_a,
                                      sync, notifier)
    for _ in range(FAILURE_ALERT_THRESHOLD + 2):
        loop.run_once()
    assert notifier.calls == []
    assert sync.calls == FAILURE_ALERT_THRESHOLD + 2  # синк реально звался


# ── C-2: успешный цикл штампует clinic.gcal_last_sync_at (для /health) ──────

def _last_sync_at(admin_engine, clinic_id):
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT gcal_last_sync_at FROM clinic WHERE id = :c"),
            {"c": clinic_id},
        ).scalar_one()


def test_success_stamps_last_sync(app_session_factory, admin_engine, clinic_a):
    sync, notifier = _Sync(fail=False), RecordingNotifier()
    loop = _loop_with_calendar_doctor(app_session_factory, admin_engine, clinic_a,
                                      sync, notifier)
    assert _last_sync_at(admin_engine, clinic_a) is None
    loop.run_once()
    assert _last_sync_at(admin_engine, clinic_a) is not None


def test_failure_does_not_stamp(app_session_factory, admin_engine, clinic_a):
    sync, notifier = _Sync(fail=True), RecordingNotifier()
    loop = _loop_with_calendar_doctor(app_session_factory, admin_engine, clinic_a,
                                      sync, notifier)
    loop.run_once()
    assert _last_sync_at(admin_engine, clinic_a) is None


# ── C-6: OAuth-сбой алертится сразу (сам не чинится), раз в день ────────────

from navbat.calendar.api import CalendarAuthError


class _AuthDeadSync:
    def __init__(self) -> None:
        self.fail = True

    def sync_doctor(self, doctor_id) -> None:
        if self.fail:
            raise CalendarAuthError("refresh не удался (400): invalid_grant")


def test_auth_error_alerts_immediately(app_session_factory, admin_engine, clinic_a):
    sync, notifier = _AuthDeadSync(), RecordingNotifier()
    loop = _loop_with_calendar_doctor(app_session_factory, admin_engine, clinic_a,
                                      sync, notifier)
    loop.run_once()  # ПЕРВЫЙ же цикл — не ждём порога 3
    assert len(notifier.calls) == 1
    assert "переавторизац" in notifier.calls[0][1]


def test_auth_alert_once_per_day_no_threshold_duplicate(app_session_factory,
                                                        admin_engine, clinic_a):
    sync, notifier = _AuthDeadSync(), RecordingNotifier()
    loop = _loop_with_calendar_doctor(app_session_factory, admin_engine, clinic_a,
                                      sync, notifier)
    for _ in range(FAILURE_ALERT_THRESHOLD + 2):
        loop.run_once()
    # один auth-алерт; генерический порог-алерт НЕ дублирует его
    assert len(notifier.calls) == 1


def test_auth_recovery_notifies_and_rearms(app_session_factory, admin_engine,
                                           clinic_a):
    sync, notifier = _AuthDeadSync(), RecordingNotifier()
    loop = _loop_with_calendar_doctor(app_session_factory, admin_engine, clinic_a,
                                      sync, notifier)
    loop.run_once()
    sync.fail = False
    loop.run_once()  # восстановление (переавторизовали)
    assert len(notifier.calls) == 2
    assert "восстановлена" in notifier.calls[1][1]
    sync.fail = True
    loop.run_once()  # умер снова в тот же день — алертим опять
    assert len(notifier.calls) == 3
