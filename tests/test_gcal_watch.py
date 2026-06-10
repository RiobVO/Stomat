"""Watch-каналы Google Calendar (C-6): открытие, продление, деградация.

Сбой watch НЕ критичен (поллинг прикрывает) — менеджер не бросает и не алертит.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from conftest import make_doctor
from navbat.calendar.api import CalendarAPIError
from navbat.calendar.watch import RENEW_LEAD, GcalWatchManager

BASE = "https://clinic.example.uz"
NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)


class FakeWatchAPI:
    def __init__(self, fail: bool = False, expiration_ms: int | None = None) -> None:
        self.fail = fail
        self.expiration_ms = expiration_ms
        self.watch_calls: list[tuple[str, str, str]] = []
        self.stop_calls: list[tuple[str, str]] = []

    def watch_events(self, calendar_id, channel_id, address):
        self.watch_calls.append((calendar_id, channel_id, address))
        if self.fail:
            raise CalendarAPIError("push недоступен (домен не верифицирован)")
        result = {"resourceId": f"RES-{len(self.watch_calls)}"}
        if self.expiration_ms is not None:
            result["expiration"] = str(self.expiration_ms)
        return result

    def stop_channel(self, channel_id, resource_id):
        self.stop_calls.append((channel_id, resource_id))


def _calendar_doctor(admin_engine, clinic_id, **fields):
    doctor_id = make_doctor(admin_engine, clinic_id)
    sets = ", ".join(f"{name} = :{name}" for name in fields)
    with admin_engine.begin() as conn:
        conn.execute(text(
            "UPDATE doctor SET gcal_calendar_id = 'cal@x'"
            + (f", {sets}" if sets else "") + " WHERE id = :doctor_id"),
            {"doctor_id": doctor_id, **fields})
    return doctor_id


def _watch_row(admin_engine, doctor_id):
    with admin_engine.begin() as conn:
        return conn.execute(text(
            "SELECT gcal_channel_id, gcal_resource_id, gcal_channel_expires_at "
            "FROM doctor WHERE id = :d"), {"d": doctor_id}).one()


def _manager(app_session_factory, clinic_id, api):
    return GcalWatchManager(app_session_factory, clinic_id, api, BASE + "/",
                            clock=lambda: NOW)


def test_opens_channel_and_stores_fields(app_session_factory, admin_engine, clinic_a):
    doctor_id = _calendar_doctor(admin_engine, clinic_a)
    expiration = NOW + timedelta(days=7)
    api = FakeWatchAPI(expiration_ms=int(expiration.timestamp() * 1000))
    _manager(app_session_factory, clinic_a, api).ensure_channels()

    assert len(api.watch_calls) == 1
    calendar_id, channel_id, address = api.watch_calls[0]
    assert calendar_id == "cal@x"
    assert address == f"{BASE}/gcal/push/{channel_id}"
    row = _watch_row(admin_engine, doctor_id)
    assert row.gcal_channel_id == channel_id
    assert row.gcal_resource_id == "RES-1"
    assert row.gcal_channel_expires_at == expiration


def test_fresh_channel_not_reopened(app_session_factory, admin_engine, clinic_a):
    _calendar_doctor(admin_engine, clinic_a,
                     gcal_channel_id="CH-OLD", gcal_resource_id="RES-OLD",
                     gcal_channel_expires_at=NOW + RENEW_LEAD + timedelta(hours=1))
    api = FakeWatchAPI()
    _manager(app_session_factory, clinic_a, api).ensure_channels()
    assert api.watch_calls == []
    assert api.stop_calls == []


def test_expiring_channel_renewed_and_old_stopped(app_session_factory, admin_engine,
                                                  clinic_a):
    doctor_id = _calendar_doctor(
        admin_engine, clinic_a,
        gcal_channel_id="CH-OLD", gcal_resource_id="RES-OLD",
        gcal_channel_expires_at=NOW + timedelta(hours=1))  # < RENEW_LEAD
    api = FakeWatchAPI(expiration_ms=int((NOW + timedelta(days=7)).timestamp() * 1000))
    _manager(app_session_factory, clinic_a, api).ensure_channels()

    assert len(api.watch_calls) == 1
    assert api.stop_calls == [("CH-OLD", "RES-OLD")]
    row = _watch_row(admin_engine, doctor_id)
    assert row.gcal_channel_id != "CH-OLD"


def test_watch_failure_degrades_to_polling(app_session_factory, admin_engine,
                                           clinic_a):
    doctor_id = _calendar_doctor(admin_engine, clinic_a)
    api = FakeWatchAPI(fail=True)
    _manager(app_session_factory, clinic_a, api).ensure_channels()  # не бросает
    row = _watch_row(admin_engine, doctor_id)
    assert row.gcal_channel_id is None  # ничего не записали — поллинг как раньше


def test_doctor_without_calendar_ignored(app_session_factory, admin_engine, clinic_a):
    make_doctor(admin_engine, clinic_a)  # gcal_calendar_id IS NULL
    api = FakeWatchAPI()
    _manager(app_session_factory, clinic_a, api).ensure_channels()
    assert api.watch_calls == []
