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


# ── К-3: вход «встать в очередь» из «нет слотов» ─────────────────────────────

def _no_slots_engine(app_session_factory, admin_engine, clinic_a):
    """Клиника с врачом БЕЗ расписания → слотов нет никогда."""
    from conftest import at_tashkent, make_doctor, next_monday
    from test_dialog_booking import explicit, extr
    from test_inline_calendar import make
    # пустые дни (truthy dict — иначе make_doctor подставит полное расписание)
    no_days = {d: [] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
    make_doctor(admin_engine, clinic_a, intervals=no_days)
    monday = next_monday()
    engine, _ = make(app_session_factory, clinic_a,
                     [extr(service="cleaning", date_ref=explicit(monday))],
                     clock=lambda: at_tashkent(monday, "08:00"))
    engine.handle_action(CHAT, "lang:ru")
    return engine


def _actions(reply):
    return [b.action for row in (reply.button_rows or ()) for b in row] + \
           [b.action for b in reply.buttons]


def test_no_slots_offers_join_button(app_session_factory, admin_engine,
                                     clinic_a, service_cleaning):
    engine = _no_slots_engine(app_session_factory, admin_engine, clinic_a)
    reply = engine.handle_text(CHAT, "чистку в понедельник")
    assert "wl:join:cleaning" in _actions(reply), "кнопка очереди в «нет слотов»"


def test_join_creates_waiting_and_idempotent(app_session_factory, admin_engine,
                                             clinic_a, service_cleaning):
    engine = _no_slots_engine(app_session_factory, admin_engine, clinic_a)
    engine.handle_text(CHAT, "чистку в понедельник")  # нет слотов
    r1 = engine.handle_action(CHAT, "wl:join:cleaning")
    r2 = engine.handle_action(CHAT, "wl:join:cleaning")
    assert "очеред" in r1.text and "уже" in r2.text
    assert rows_in_db(admin_engine, clinic_a) == [(CHAT, "waiting")]


# ── К-4: матчер ──────────────────────────────────────────────────────────────

def _matcher(app_session_factory, clinic_id):
    from test_reminders import make_service_obj
    return make_service_obj(app_session_factory, clinic_id)


def age_notified(admin_engine, clinic_id, hours):
    with admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE waitlist SET notified_at = now() - "
                 "make_interval(hours => :h) WHERE clinic_id = :c"),
            {"h": hours, "c": clinic_id})


def test_match_notifies_with_leave_button_and_marks_notified(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    # пациент в очереди + врач с расписанием (слоты есть) → пуш
    with tenant_transaction(app_session_factory, clinic_a) as s:
        wl.add(s, service_cleaning, CHAT, None, "ru")
    service, api, _ = _matcher(app_session_factory, clinic_a)
    assert service.match_waitlist() == 1
    actions = [b.action for row in api.row_keyboards[-1] for b in row]
    assert any(a.startswith("wl:leave:") for a in actions), "кнопка выхода (сырая)"
    assert any(a.startswith("a:") for a in actions), "slot:-кнопка (нумерована)"
    assert rows_in_db(admin_engine, clinic_a) == [(CHAT, "notified")]


def test_renotify_cooldown(app_session_factory, admin_engine, clinic_a,
                           doctor_a, service_cleaning):
    with tenant_transaction(app_session_factory, clinic_a) as s:
        wl.add(s, service_cleaning, CHAT, None, "ru")
    service, api, _ = _matcher(app_session_factory, clinic_a)
    service.match_waitlist()
    assert service.match_waitlist() == 0, "кулдаун: сразу повторно не шлём"
    age_notified(admin_engine, clinic_a, 7)  # > WAITLIST_RENOTIFY_HOURS
    assert service.match_waitlist() == 1, "после кулдауна шлём снова"


def test_match_empty_queue_no_send(app_session_factory, admin_engine, clinic_a,
                                   doctor_a, service_cleaning):
    service, api, _ = _matcher(app_session_factory, clinic_a)
    assert service.match_waitlist() == 0
    assert api.sent == []


def test_match_no_free_slot_keeps_waiting(app_session_factory, admin_engine,
                                          clinic_a, service_cleaning):
    no_days = {d: [] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
    from conftest import make_doctor
    make_doctor(admin_engine, clinic_a, intervals=no_days)  # слотов нет
    with tenant_transaction(app_session_factory, clinic_a) as s:
        wl.add(s, service_cleaning, CHAT, None, "ru")
    service, api, _ = _matcher(app_session_factory, clinic_a)
    assert service.match_waitlist() == 0
    assert api.sent == []
    assert rows_in_db(admin_engine, clinic_a) == [(CHAT, "waiting")]


# ── К-5: снятие / fulfillment / гонки ────────────────────────────────────────

def _slot_action(admin_engine, clinic_id):
    """Достать slot:-action из tg_actions-map conversation (после пуша)."""
    import json
    with admin_engine.begin() as conn:
        ctx = conn.execute(
            text("SELECT context FROM waitlist w JOIN conversation c "
                 "ON c.tg_chat_id = w.tg_chat_id WHERE w.clinic_id = :cid "
                 "LIMIT 1"), {"cid": clinic_id}).scalar_one()
    actions = (ctx.get("tg_actions") or {}) if isinstance(ctx, dict) \
        else json.loads(ctx).get("tg_actions", {})
    return next(a for a in actions.values() if a.startswith("slot:"))


def test_slot_tap_from_offer_books_and_fulfills(app_session_factory, admin_engine,
                                                clinic_a, doctor_a,
                                                service_cleaning):
    from navbat.dialog.fsm import DialogEngine
    from navbat.dialog.patients import create_patient
    from navbat.nlu.extractor import FakeExtractor
    # известный пациент: тап слота → запись сразу (без имени/телефона)
    with tenant_transaction(app_session_factory, clinic_a) as s:
        create_patient(s, CHAT, "Пациент", "998901112233")
        wl.add(s, service_cleaning, CHAT, None, "ru")
    service, api, _ = _matcher(app_session_factory, clinic_a)
    assert service.match_waitlist() == 1
    slot = _slot_action(admin_engine, clinic_a)

    engine = DialogEngine(app_session_factory, clinic_a,
                          extractor=FakeExtractor(script=[]))
    reply = engine.handle_action(CHAT, slot)
    assert "✅" in reply.text or "подтвержд" in reply.text.lower() \
        or "ЗАПИСЬ" in reply.text
    with admin_engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM appointment "
                                 "WHERE status='booked'")).scalar_one() == 1
    # следующий цикл матчера снимает с очереди (записался)
    service.match_waitlist()
    assert rows_in_db(admin_engine, clinic_a) == [(CHAT, "fulfilled")]


def test_auto_fulfilled_if_booked_elsewhere(app_session_factory, admin_engine,
                                            clinic_a, doctor_a, service_cleaning):
    from conftest import next_monday
    with tenant_transaction(app_session_factory, clinic_a) as s:
        wl.add(s, service_cleaning, CHAT, None, "ru")
    book(app_session_factory, clinic_a, doctor_a, service_cleaning,
         next_monday(), "09:00", chat_id=CHAT)  # записался обычным путём
    service, api, _ = _matcher(app_session_factory, clinic_a)
    assert service.match_waitlist() == 0, "не уведомляем — уже записан"
    assert rows_in_db(admin_engine, clinic_a) == [(CHAT, "fulfilled")]


def test_wl_leave_cancels(app_session_factory, admin_engine, clinic_a,
                          service_cleaning):
    from navbat.dialog.fsm import DialogEngine
    from navbat.nlu.extractor import FakeExtractor
    with tenant_transaction(app_session_factory, clinic_a) as s:
        wid = wl.add(s, service_cleaning, CHAT, None, "ru")
    engine = DialogEngine(app_session_factory, clinic_a,
                          extractor=FakeExtractor(script=[]))
    engine.handle_action(CHAT, f"wl:leave:{wid}")
    assert rows_in_db(admin_engine, clinic_a) == [(CHAT, "cancelled")]


def test_chat_unavailable_drops_from_queue(app_session_factory, admin_engine,
                                           clinic_a, doctor_a, service_cleaning):
    with tenant_transaction(app_session_factory, clinic_a) as s:
        wl.add(s, service_cleaning, CHAT, None, "ru")
    service, api, _ = _matcher(app_session_factory, clinic_a)
    api.chat_gone = True  # пациент заблокировал бота
    assert service.match_waitlist() == 0
    assert rows_in_db(admin_engine, clinic_a) == [(CHAT, "cancelled")]
