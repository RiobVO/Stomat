"""Выбор даты: месячная inline-сетка с маркерами (вариант B) + day-view.

Пересмотр 11.06 (живой тык, полировка-3): список доступных дней (П-5б)
заменён месячной сеткой — заголовок «Июнь 2026», ряды пн–вс, свободный
день «•15», занятый/прошлый «15» → toast, навигация «◀»/«▶» по месяцам
edit'ом. Чистое month_view — без БД; сценарии — DialogEngine с
инжектированными часами. Сообщение с сеткой живёт долго — устаревшие
клики (включая legacy-callback'и списка дат и старой сетки) не падают.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta

from sqlalchemy import text

from conftest import at_tashkent, make_service, next_monday
from navbat.dialog.calendar_view import HORIZON_DAYS, month_title, month_view
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


def page_days(reply):
    return [date.fromisoformat(b.action.split(":")[2])
            for b in flat(reply.button_rows) if b.action.startswith("cal:day:")]


def nav_buttons(reply_rows):
    return {b.label: b for b in flat(reply_rows)
            if b.action.startswith("cal:nav:")}


# ── Чистый month_view ────────────────────────────────────────────────────────

TODAY = date(2026, 6, 11)                          # четверг
HORIZON_END = TODAY + timedelta(days=HORIZON_DAYS)  # 2026-09-09
JUNE = date(2026, 6, 1)                            # понедельник


def test_month_view_grid_layout():
    free = {date(2026, 6, 15), date(2026, 6, 16)}
    caption, rows = month_view(JUNE, free, TODAY, HORIZON_END, "ru")

    assert "Июнь 2026" in caption
    assert [b.label for b in rows[0]] == ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    assert all(b.action == "cal:noop" for b in rows[0]), "шапка дней — заглушки"
    day_rows = rows[1:-1]  # последний ряд — навигация
    assert [len(r) for r in day_rows] == [7] * 5, "июнь с пн: 30 дней + 5 заглушек"
    cells = {b.label: b for b in flat(day_rows)}
    assert cells["•15"].action == "cal:day:2026-06-15"
    assert cells["17"].action == "cal:noop", "день без слотов — мёртвая ячейка"
    assert cells["5"].action == "cal:noop", "прошлый день — мёртвая ячейка"
    assert [b.label for b in flat(day_rows)[-5:]] == [" "] * 5, "хвостовые заглушки"


def test_month_view_leading_fillers():
    july = date(2026, 7, 1)  # среда → две заглушки до первого дня
    _, rows = month_view(july, set(), TODAY, HORIZON_END, "ru")
    first_week = rows[1]
    assert [b.label for b in first_week[:3]] == [" ", " ", "1"]
    assert all(b.action == "cal:noop" for b in first_week[:2])


def test_month_view_nav_buttons():
    # текущий месяц: в прошлое не листаемся
    _, rows = month_view(JUNE, set(), TODAY, HORIZON_END, "ru")
    nav = nav_buttons(rows)
    assert list(nav) == ["▶"]
    assert nav["▶"].action == "cal:nav:2026-07-01"

    # середина горизонта: обе стрелки
    _, rows = month_view(date(2026, 7, 1), set(), TODAY, HORIZON_END, "ru")
    assert list(nav_buttons(rows)) == ["◀", "▶"]

    # последний месяц горизонта: вперёд нельзя
    _, rows = month_view(date(2026, 9, 1), set(), TODAY, HORIZON_END, "ru")
    nav = nav_buttons(rows)
    assert list(nav) == ["◀"]
    assert nav["◀"].action == "cal:nav:2026-08-01"


def test_month_view_empty_month_note():
    caption, rows = month_view(JUNE, set(), TODAY, HORIZON_END, "ru")
    assert TEMPLATES["cal_no_free_days_month"]["ru"] in caption
    assert not [b for b in flat(rows) if b.action.startswith("cal:day:")]


def test_month_view_uzbek():
    caption, rows = month_view(JUNE, {date(2026, 6, 15)}, TODAY, HORIZON_END, "uz")
    assert "Iyun 2026" in caption
    assert TEMPLATES["cal_no_free_days_month"]["uz"] not in caption
    assert [b.label for b in rows[0]] == ["du", "se", "ch", "pa", "ju", "sh", "ya"]


# ── Вход: «📅 Выбрать дату» в ask_date ───────────────────────────────────────

def test_ask_date_has_calendar_button(app_session_factory, admin_engine,
                                      clinic_a, doctor_a, service_cleaning):
    monday = next_monday()
    engine, _ = make(app_session_factory, clinic_a, [extr(intent="question")],
                     clock=lambda: at_tashkent(monday, "08:00"))
    reply = engine.handle_text(CHAT, "а есть свободные окошки?")

    cal_buttons = [b for b in reply.buttons if b.action.startswith("cal:nav:")]
    assert cal_buttons and cal_buttons[0].label == TEMPLATES["btn_pick_date"]["ru"]
    assert cal_buttons[0].action == f"cal:nav:{monday.isoformat()}"


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


# ── Сетка месяца через flow ──────────────────────────────────────────────────

def test_nav_opens_month_grid_with_free_day_markers(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    make_service(admin_engine, clinic_a, "checkup", 30)  # дефолт без услуги
    monday = next_monday()
    engine, notifier = make(app_session_factory, clinic_a, [],
                            clock=lambda: at_tashkent(monday, "08:00"))
    reply = engine.handle_action(CHAT, f"cal:nav:{monday.isoformat()}")

    assert reply.edit, "сетка редактирует сообщение"
    first = monday.replace(day=1)
    assert month_title(first, "ru") in reply.text
    day_rows = [row for row in reply.button_rows
                if any(b.action.startswith("cal:day:") for b in row)]
    assert all(len(row) == 7 for row in day_rows), "ряды дней по 7"
    last_day = monthrange(first.year, first.month)[1]
    expected = [first.replace(day=n) for n in range(1, last_day + 1)
                if first.replace(day=n) >= monday
                and first.replace(day=n).weekday() != 6]
    assert page_days(reply) == expected, \
        "«•» ровно у рабочих дней от сегодня; прошлые и воскресенья мёртвые"
    assert notifier.calls == []


def test_month_nav_forward_edits_and_back_not_past(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    make_service(admin_engine, clinic_a, "checkup", 30)
    monday = next_monday()
    engine, _ = make(app_session_factory, clinic_a, [],
                     clock=lambda: at_tashkent(monday, "08:00"))
    first = engine.handle_action(CHAT, f"cal:nav:{monday.isoformat()}")
    nav = nav_buttons(first.button_rows)
    assert "◀" not in nav, "с текущего месяца в прошлое не листаемся"

    second = engine.handle_action(CHAT, nav["▶"].action)
    assert second.edit
    next_month = (monday.replace(day=1) + timedelta(days=32)).replace(day=1)
    assert month_title(next_month, "ru") in second.text

    # «◀» со следующего месяца ведёт в 1-е число (прошлое) → кламп к сегодня
    again = engine.handle_action(CHAT, nav_buttons(second.button_rows)["◀"].action)
    assert month_title(monday.replace(day=1), "ru") in again.text
    assert page_days(again) == page_days(first)


def test_last_horizon_month_has_no_forward(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    make_service(admin_engine, clinic_a, "checkup", 30)
    monday = next_monday()
    engine, _ = make(app_session_factory, clinic_a, [],
                     clock=lambda: at_tashkent(monday, "08:00"))
    horizon_end = monday + timedelta(days=HORIZON_DAYS)
    reply = engine.handle_action(CHAT, f"cal:nav:{horizon_end.isoformat()}")

    assert month_title(horizon_end.replace(day=1), "ru") in reply.text
    nav = nav_buttons(reply.button_rows)
    assert "▶" not in nav and "◀" in nav


def test_nav_outside_horizon_resets_to_current_month(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    make_service(admin_engine, clinic_a, "checkup", 30)
    monday = next_monday()
    engine, _ = make(app_session_factory, clinic_a, [],
                     clock=lambda: at_tashkent(monday, "08:00"))
    far = monday + timedelta(days=200)
    reply = engine.handle_action(CHAT, f"cal:nav:{far.isoformat()}")
    assert month_title(monday.replace(day=1), "ru") in reply.text
    assert page_days(reply), "за горизонтом — сетка текущего месяца"


def test_legacy_iso_nav_opens_month_of_date(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    # старые сообщения списка дат (П-5б) шлют cal:nav:<ISO-дата>
    make_service(admin_engine, clinic_a, "checkup", 30)
    monday = next_monday()
    engine, _ = make(app_session_factory, clinic_a, [],
                     clock=lambda: at_tashkent(monday, "08:00"))
    target = monday + timedelta(days=45)
    reply = engine.handle_action(CHAT, f"cal:nav:{target.isoformat()}")
    assert month_title(target.replace(day=1), "ru") in reply.text


def test_legacy_month_nav_still_works(app_session_factory, admin_engine,
                                      clinic_a, doctor_a, service_cleaning):
    # совсем старые сообщения первой сетки (П-5) шлют cal:nav:YYYY-MM
    make_service(admin_engine, clinic_a, "checkup", 30)
    monday = next_monday()
    engine, _ = make(app_session_factory, clinic_a, [],
                     clock=lambda: at_tashkent(monday, "08:00"))
    reply = engine.handle_action(CHAT, f"cal:nav:{monday:%Y-%m}")
    assert page_days(reply), "legacy-формат не падает и даёт сетку месяца"


def test_nav_garbage_is_stale(app_session_factory, admin_engine, clinic_a,
                              doctor_a, service_cleaning):
    monday = next_monday()
    engine, _ = make(app_session_factory, clinic_a, [],
                     clock=lambda: at_tashkent(monday, "08:00"))
    reply = engine.handle_action(CHAT, "cal:nav:зюзя")
    assert TEMPLATES["stale_button"]["ru"] in reply.text


def test_noop_and_none_cells_toast(app_session_factory, admin_engine, clinic_a,
                                   doctor_a, service_cleaning):
    # пересмотр 11.06 (возврат сетки): занятые/прошлые ячейки шлют cal:noop —
    # пациент получает toast, не молчание; legacy cal:none живёт так же
    monday = next_monday()
    engine, _ = make(app_session_factory, clinic_a, [],
                     clock=lambda: at_tashkent(monday, "08:00"))
    for action in ("cal:noop", "cal:none"):
        reply = engine.handle_action(CHAT, action)
        assert reply.text == ""
        assert reply.toast == TEMPLATES["cal_no_slots"]["ru"]


def test_empty_month_grid_shows_note(app_session_factory, admin_engine,
                                     clinic_a, doctor_a, service_cleaning):
    # месяц без свободных дней, но горизонт не пуст → сетка без «•» + строка
    make_service(admin_engine, clinic_a, "checkup", 30)
    monday = next_monday()
    engine, _ = make(app_session_factory, clinic_a, [],
                     clock=lambda: at_tashkent(monday, "08:00"))
    first = monday.replace(day=1)
    last_day = first.replace(day=monthrange(first.year, first.month)[1])
    with admin_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO holiday (clinic_id, date) "
            "SELECT :cid, d::date FROM generate_series("
            "CAST(:a AS timestamp), CAST(:b AS timestamp), '1 day') d"),
            {"cid": clinic_a, "a": monday, "b": last_day})

    reply = engine.handle_action(CHAT, f"cal:nav:{monday.isoformat()}")
    assert TEMPLATES["cal_no_free_days_month"]["ru"] in reply.text
    assert not page_days(reply), "ни одной «•»-кнопки"
    assert "▶" in nav_buttons(reply.button_rows), "дальше листать можно"


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
    assert back.label == TEMPLATES["btn_back_calendar"]["ru"]
    assert back.action == f"cal:nav:{monday.isoformat()}"
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
    assert reply.edit and page_days(reply), "перерисована свежая сетка месяца"


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


# ── «Нет слотов на 2 недели» → сетка/честный текст + FYI раз в день ──────────

def _drop_schedule(admin_engine):
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE doctor SET working_intervals = '{}'"))


def test_no_slots_anywhere_honest_text_no_dead_buttons(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    _drop_schedule(admin_engine)
    monday = next_monday()
    engine, notifier = make(
        app_session_factory, clinic_a,
        [extr(service="cleaning", date_ref=explicit(monday)),
         extr(service="cleaning", date_ref=explicit(monday))],
        clock=lambda: at_tashkent(monday, "08:00"))

    reply = engine.handle_text(CHAT, "чистка в понедельник")
    assert TEMPLATES["no_slots_horizon"]["ru"] in reply.text
    assert not reply.button_rows, "мёртвых кнопок нет"
    assert fsm_state(admin_engine) == "booking_collect", "диалог жив"
    assert len(notifier.calls) == 1, "владельцу FYI"

    engine.handle_text(CHAT, "а на чистку в понедельник?")  # второй заход
    assert len(notifier.calls) == 1, "FYI раз в день, не на каждый заход"
