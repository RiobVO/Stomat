"""Инлайн-календарь (П-5): эмодзи-сетка месяца, day-view, навигация.

Чистый month_view — без БД; сценарные тесты — DialogEngine с
инжектированными часами. Кликабельны только дни со слотами; сообщение
календаря живёт долго — устаревшие клики валидируются, не падают.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import text

from conftest import at_tashkent, make_service, next_monday
from navbat.dialog.calendar_view import BLANK, month_view
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.replies import TEMPLATES
from navbat.nlu.extractor import FakeExtractor
from test_dialog_booking import (
    CHAT, RecordingNotifier, explicit, extr, fsm_state)
from test_dialog_reschedule_cancel import book_directly


def make(app_session_factory, clinic_id, script, clock):
    notifier = RecordingNotifier()
    engine = DialogEngine(app_session_factory, clinic_id,
                          extractor=FakeExtractor(script=script),
                          notifier=notifier, clock=clock)
    return engine, notifier


def flat(rows):
    return [b for row in rows for b in row]


def day_actions(reply):
    return [b.action for b in flat(reply.button_rows)
            if b.action.startswith("cal:day:")]


# ── Чистый month_view ────────────────────────────────────────────────────────

TODAY = date(2026, 6, 11)  # четверг


def test_month_view_header_and_padding():
    # июль 2026: 1.07 — среда, паддинг Пн+Вт пустой и некликабельный
    caption, rows = month_view(2026, 7, set(), TODAY, "ru")

    assert "Июль 2026" in caption and "🟢" in caption
    assert [b.label for b in rows[0]] == ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    first_week = rows[1]
    assert first_week[0].label == BLANK and first_week[0].action == "cal:noop"
    assert first_week[1].action == "cal:noop"


def test_month_view_marks_available_today_and_empty():
    available = {date(2026, 6, 11), date(2026, 6, 15)}
    _, rows = month_view(2026, 6, available, TODAY, "ru")
    cells = {b.label: b.action for b in flat(rows)}

    assert cells["📍11"] == "cal:day:2026-06-11", "сегодня со слотами — 📍"
    assert cells["🟢15"] == "cal:day:2026-06-15"
    # будущий день без слотов: пустая ячейка, клик даёт toast (cal:none)
    future_blank = [b for b in flat(rows)
                    if b.label == BLANK and b.action == "cal:none"]
    assert future_blank, "будущие дни без слотов кликаются в toast"
    # прошедшие дни месяца — глухие
    week_with_first = rows[1]
    assert all(b.action == "cal:noop" for b in week_with_first[:4])


def test_month_view_nav_first_middle_last():
    _, rows_first = month_view(2026, 6, set(), TODAY, "ru")
    nav = [b.action for b in rows_first[-1]]
    assert nav == ["cal:nav:2026-07"], "на текущем месяце нет ◀"

    _, rows_mid = month_view(2026, 7, set(), TODAY, "ru")
    assert [b.action for b in rows_mid[-1]] == \
        ["cal:nav:2026-06", "cal:nav:2026-08"]

    _, rows_last = month_view(2026, 8, set(), TODAY, "ru")
    assert [b.action for b in rows_last[-1]] == ["cal:nav:2026-07"], \
        "на последнем месяце горизонта нет ▶"


def test_month_view_uzbek():
    caption, rows = month_view(2026, 6, {date(2026, 6, 15)}, TODAY, "uz")
    assert "Iyun 2026" in caption and "bo'sh" in caption
    assert [b.label for b in rows[0]] == ["Du", "Se", "Cho", "Pa", "Ju", "Sha", "Ya"]
    assert any("Iyul ▶" == b.label for b in rows[-1])


# ── Вход: «📅 Выбрать дату» в ask_date ───────────────────────────────────────

def test_ask_date_has_calendar_button(app_session_factory, admin_engine,
                                      clinic_a, doctor_a, service_cleaning):
    monday = next_monday()
    engine, _ = make(app_session_factory, clinic_a, [extr(intent="question")],
                     clock=lambda: at_tashkent(monday, "08:00"))
    reply = engine.handle_text(CHAT, "а есть свободные окошки?")

    cal_buttons = [b for b in reply.buttons if b.action.startswith("cal:nav:")]
    assert cal_buttons and cal_buttons[0].label == TEMPLATES["btn_pick_date"]["ru"]
    assert cal_buttons[0].action == f"cal:nav:{monday:%Y-%m}"


def test_booking_date_step_has_calendar_button(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    # «Записаться» → услуга → вопрос дня ДОЛЖЕН содержать календарь
    # (живой тык 11.06: кнопки не было на happy path выбора даты)
    monday = next_monday()
    engine, _ = make(app_session_factory, clinic_a, [],
                     clock=lambda: at_tashkent(monday, "08:00"))
    engine.handle_action(CHAT, "lang:ru")
    engine.handle_text(CHAT, TEMPLATES["btn_menu_book"]["ru"])  # меню
    reply = engine.handle_action(CHAT, "service:cleaning")

    assert any(b.action.startswith("cal:nav:") for b in reply.buttons), \
        "шаг «на какой день?» содержит «📅 Выбрать дату»"


def test_reschedule_date_step_has_calendar_button(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    monday = next_monday()
    book_directly(app_session_factory, clinic_a, doctor_a, service_cleaning,
                  monday, "09:00")
    engine, _ = make(app_session_factory, clinic_a,
                     [extr(intent="reschedule")],  # без даты
                     clock=lambda: at_tashkent(monday, "08:00"))
    reply = engine.handle_text(CHAT, "перенесите мою запись")

    assert any(b.action.startswith("cal:nav:") for b in reply.buttons)
    assert fsm_state(admin_engine) == "resched_offer_slots"


# ── Навигация и сетка ────────────────────────────────────────────────────────

def test_nav_renders_month_with_working_days_only(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    make_service(admin_engine, clinic_a, "checkup", 30)  # дефолт без услуги
    monday = next_monday()
    engine, notifier = make(app_session_factory, clinic_a, [],
                            clock=lambda: at_tashkent(monday, "08:00"))
    reply = engine.handle_action(CHAT, f"cal:nav:{monday:%Y-%m}")

    assert reply.edit, "навигация редактирует сообщение, не шлёт новое"
    days = [date.fromisoformat(a.split(":")[2]) for a in day_actions(reply)]
    assert days, "в месяце есть кликабельные дни"
    assert all(d.weekday() != 6 for d in days), "воскресений нет в графике"
    assert all(d >= monday for d in days)
    assert notifier.calls == []


def test_nav_outside_horizon_redraws_current(app_session_factory, admin_engine,
                                             clinic_a, doctor_a, service_cleaning):
    monday = next_monday()
    engine, _ = make(app_session_factory, clinic_a, [],
                     clock=lambda: at_tashkent(monday, "08:00"))
    far = monday + timedelta(days=200)
    reply = engine.handle_action(CHAT, f"cal:nav:{far:%Y-%m}")
    # за горизонтом — перерисовываем текущий месяц
    assert f"{monday.year}" in reply.text
    assert reply.edit


def test_nav_garbage_is_stale(app_session_factory, admin_engine, clinic_a,
                              doctor_a, service_cleaning):
    monday = next_monday()
    engine, _ = make(app_session_factory, clinic_a, [],
                     clock=lambda: at_tashkent(monday, "08:00"))
    reply = engine.handle_action(CHAT, "cal:nav:зюзя")
    assert TEMPLATES["stale_button"]["ru"] in reply.text


def test_noop_and_none_cells(app_session_factory, admin_engine, clinic_a,
                             doctor_a, service_cleaning):
    monday = next_monday()
    engine, _ = make(app_session_factory, clinic_a, [],
                     clock=lambda: at_tashkent(monday, "08:00"))
    silent = engine.handle_action(CHAT, "cal:noop")
    assert silent.text == "" and silent.toast is None

    toast = engine.handle_action(CHAT, "cal:none")
    assert toast.text == ""
    assert toast.toast == TEMPLATES["cal_no_slots"]["ru"]


# ── Day-view ─────────────────────────────────────────────────────────────────

def test_day_click_shows_all_slots_in_grid(app_session_factory, admin_engine,
                                           clinic_a, doctor_a, service_cleaning):
    monday = next_monday()
    engine, _ = make(app_session_factory, clinic_a,
                     [extr(service="cleaning", date_ref=explicit(monday))],
                     clock=lambda: at_tashkent(monday, "08:00"))
    engine.handle_text(CHAT, "чистка в понедельник")  # услуга в контексте

    reply = engine.handle_action(CHAT, f"cal:day:{monday.isoformat()}")
    assert reply.edit
    slot_btns = [b for b in flat(reply.button_rows)
                 if b.action.startswith("slot:")]
    # график 09–13/14–18, 30-мин слоты → все 16, не SLOTS_PER_REPLY
    assert len(slot_btns) == 16
    assert all(len(row) <= 4 for row in reply.button_rows)
    back = flat(reply.button_rows)[-1]
    assert back.action == f"cal:nav:{monday:%Y-%m}"
    assert fsm_state(admin_engine) == "booking_offer_slots"

    # дальше — штатный путь: выбор слота берёт hold
    engine.handle_action(CHAT, slot_btns[0].action)
    assert fsm_state(admin_engine) == "awaiting_name"


def test_past_day_click_toasts_and_redraws(app_session_factory, admin_engine,
                                           clinic_a, doctor_a, service_cleaning):
    make_service(admin_engine, clinic_a, "checkup", 30)
    monday = next_monday()
    engine, _ = make(app_session_factory, clinic_a, [],
                     clock=lambda: at_tashkent(monday, "08:00"))
    past = monday - timedelta(days=7)
    reply = engine.handle_action(CHAT, f"cal:day:{past.isoformat()}")

    assert reply.toast == TEMPLATES["cal_past_day"]["ru"]
    assert reply.edit and day_actions(reply), "перерисован свежий месяц"


def test_day_click_in_reschedule_keeps_resched(app_session_factory, admin_engine,
                                               clinic_a, doctor_a, service_cleaning):
    monday = next_monday()
    tuesday = monday + timedelta(days=1)
    book_directly(app_session_factory, clinic_a, doctor_a, service_cleaning,
                  monday, "09:00")
    engine, _ = make(app_session_factory, clinic_a,
                     [extr(intent="reschedule", date_ref=explicit(monday))],
                     clock=lambda: at_tashkent(monday, "08:00"))
    engine.handle_text(CHAT, "перенесите запись")
    assert fsm_state(admin_engine) == "resched_offer_slots"

    reply = engine.handle_action(CHAT, f"cal:day:{tuesday.isoformat()}")
    actions = [b.action for b in flat(reply.button_rows)]
    assert any(a.startswith("reslot:") for a in actions), \
        "в переносе кнопки слотов — reslot"
    assert fsm_state(admin_engine) == "resched_offer_slots"


# ── «Нет слотов на 2 недели» → календарь + FYI раз в день ───────────────────

def _drop_schedule(admin_engine):
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE doctor SET working_intervals = '{}'"))


def test_no_slots_offers_calendar_not_escalation(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    _drop_schedule(admin_engine)
    monday = next_monday()
    engine, notifier = make(
        app_session_factory, clinic_a,
        [extr(service="cleaning", date_ref=explicit(monday)),
         extr(service="cleaning", date_ref=explicit(monday))],
        clock=lambda: at_tashkent(monday, "08:00"))

    reply = engine.handle_text(CHAT, "чистка в понедельник")
    assert TEMPLATES["no_slots_calendar"]["ru"] in reply.text
    assert any(b.action.startswith("cal:nav:") for b in flat(reply.button_rows)), \
        "календарь листается даже пустой"
    assert fsm_state(admin_engine) == "booking_collect", "диалог жив"
    assert len(notifier.calls) == 1, "владельцу FYI"

    engine.handle_text(CHAT, "а на чистку в понедельник?")  # второй заход
    assert len(notifier.calls) == 1, "FYI раз в день, не на каждый заход"
