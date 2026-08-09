"""Лист ожидания: таблица + репозиторий (К-1/К-2)."""
from __future__ import annotations

import uuid

from sqlalchemy import text

from navbat.db.base import tenant_transaction
from navbat.dialog import waitlist_repo as wl
from test_gcal_export import book

CHAT = 5001


def service_id_of(admin_engine, clinic_id, name="cleaning"):
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT id FROM service WHERE clinic_id = :c AND name = :n"),
            {"c": clinic_id, "n": name}).scalar_one()


def rows_in_db(admin_engine, clinic_id):
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT tg_chat_id, status FROM waitlist WHERE clinic_id = :c "
                 "ORDER BY id"), {"c": clinic_id}).all()


def age_created(admin_engine, clinic_id, days):
    with admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE waitlist SET created_at = now() - "
                 "make_interval(days => :d) WHERE clinic_id = :c"),
            {"d": days, "c": clinic_id})


# ── К-1: таблица, дедуп, RLS ─────────────────────────────────────────────────

def test_add_and_dedup(app_session_factory, admin_engine, clinic_a,
                       service_cleaning):
    with tenant_transaction(app_session_factory, clinic_a) as s:
        first = wl.add(s, service_cleaning, CHAT, None, "uz")
        dup = wl.add(s, service_cleaning, CHAT, None, "uz")
    assert first is not None
    assert dup is None, "повторная активная запись на услугу не плодит дубль"
    assert rows_in_db(admin_engine, clinic_a) == [(CHAT, "waiting")]


def test_cancelled_does_not_block_new(app_session_factory, admin_engine,
                                      clinic_a, service_cleaning):
    with tenant_transaction(app_session_factory, clinic_a) as s:
        wid = wl.add(s, service_cleaning, CHAT, None, "ru")
        wl.mark_cancelled(s, wid)
        again = wl.add(s, service_cleaning, CHAT, None, "ru")
    assert again is not None, "после отмены можно встать в очередь заново"


def test_rls_isolation(app_session_factory, admin_engine, clinic_a, clinic_b):
    # услуга в каждой клинике своя
    from conftest import make_service
    sa = make_service(admin_engine, clinic_a, "cleaning", 30)
    sb = make_service(admin_engine, clinic_b, "cleaning", 30)
    with tenant_transaction(app_session_factory, clinic_a) as s:
        wl.add(s, sa, CHAT, None, "ru")
    with tenant_transaction(app_session_factory, clinic_b) as s:
        assert wl.count_waiting(s) == 0, "очередь клиники A не видна из B"
        wl.add(s, sb, CHAT, None, "ru")
        assert wl.count_waiting(s) == 1


# ── К-2: репозиторий ─────────────────────────────────────────────────────────

def test_list_and_count_waiting(app_session_factory, clinic_a, service_cleaning):
    with tenant_transaction(app_session_factory, clinic_a) as s:
        wl.add(s, service_cleaning, 1, None, "ru")
        wl.add(s, service_cleaning, 2, None, "uz")
        waiting = wl.list_waiting(s)
        assert [r.tg_chat_id for r in waiting] == [1, 2]  # oldest-first
        assert wl.count_waiting(s) == 2


def test_mark_notified_and_active_lookup(app_session_factory, clinic_a,
                                         service_cleaning):
    with tenant_transaction(app_session_factory, clinic_a) as s:
        wid = wl.add(s, service_cleaning, CHAT, None, "ru")
        wl.mark_notified(s, wid)
        row = wl.active_for_chat_service(s, CHAT, service_cleaning)
        assert row is not None and row.status == "notified"  # notified ещё активен
        assert wl.count_waiting(s) == 1


def test_mark_fulfilled_drops_from_active(app_session_factory, clinic_a,
                                          service_cleaning):
    with tenant_transaction(app_session_factory, clinic_a) as s:
        wid = wl.add(s, service_cleaning, CHAT, None, "ru")
        wl.mark_fulfilled(s, wid)
        assert wl.count_waiting(s) == 0
        assert wl.active_for_chat_service(s, CHAT, service_cleaning) is None


def test_expire_old(app_session_factory, admin_engine, clinic_a,
                    service_cleaning):
    with tenant_transaction(app_session_factory, clinic_a) as s:
        wl.add(s, service_cleaning, CHAT, None, "ru")
    age_created(admin_engine, clinic_a, 20)
    with tenant_transaction(app_session_factory, clinic_a) as s:
        assert wl.expire_old(s, 14) == 1
        assert wl.count_waiting(s) == 0


def test_has_future_booked(app_session_factory, admin_engine, clinic_a,
                           doctor_a, service_cleaning):
    from conftest import next_monday
    with tenant_transaction(app_session_factory, clinic_a) as s:
        assert wl.has_future_booked(s, CHAT, service_cleaning) is False
    book(app_session_factory, clinic_a, doctor_a, service_cleaning,
         next_monday(), "10:00", chat_id=CHAT)
    with tenant_transaction(app_session_factory, clinic_a) as s:
        assert wl.has_future_booked(s, CHAT, service_cleaning) is True
