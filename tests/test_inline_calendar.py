"""Выбор даты списком доступных дней (П-5/П-5б) + day-view слотов.

Редизайн по живому тыку 11.06 (паттерн маникюр-бота): кнопки — ТОЛЬКО
дни с реальными слотами, «11 июн · чт», никаких пустых ячеек. Чистый
dates_view — без БД; сценарии — DialogEngine с инжектированными часами.
Сообщение с датами живёт долго — устаревшие клики не падают.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import text

from conftest import at_tashkent, make_service, next_monday
from navbat.dialog.calendar_view import dates_view, day_label
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


# ── Чистый dates_view ────────────────────────────────────────────────────────

TODAY = date(2026, 6, 11)  # четверг


def test_dates_view_two_columns_real_days_only():
    days = [TODAY + timedelta(days=i) for i in (0, 1, 2, 4, 5)]  # вс пропущено
    caption, rows = dates_view(days, TODAY, TODAY, has_more=False, lang="ru")

    assert "Выберите день" in caption
    assert [len(row) for row in rows] == [2, 2, 1], "по 2 в ряд, без заглушек"
    first = rows[0][0]
    assert first.label == "11 июн · чт"
    assert first.action == "cal:day:2026-06-11"
    actions = [b.action for b in flat(rows)]
    assert all(a.startswith("cal:day:") for a in actions), "ни одной мёртвой кнопки"


def test_dates_view_pagination_buttons():
    days = [TODAY + timedelta(days=i) for i in range(10)]
    _, first_page = dates_view(days, TODAY, TODAY, has_more=True, lang="ru")
    nav = [b for b in flat(first_page) if b.action.startswith("cal:nav:")]
    assert [b.label for b in nav] == [TEMPLATES["btn_more_dates"]["ru"]], \
        "на первой странице нет «◀ Ближайшие»"
    following = max(days) + timedelta(days=1)
    assert nav[0].action == f"cal:nav:{following.isoformat()}"

    _, last_page = dates_view(days, TODAY + timedelta(days=30), TODAY,
                              has_more=False, lang="ru")
    nav = [b for b in flat(last_page) if b.action.startswith("cal:nav:")]
    assert [b.label for b in nav] == [TEMPLATES["btn_first_dates"]["ru"]], \
        "на последней странице нет «Ещё даты»"
    assert nav[0].action == f"cal:nav:{TODAY.isoformat()}"


def test_dates_view_uzbek_labels():
    caption, rows = dates_view([TODAY], TODAY, TODAY, has_more=False, lang="uz")
    assert "Kunni tanlang" in caption
    assert rows[0][0].label == "11 iyn · pa"
    assert day_label(date(2026, 7, 5), "uz") == "5 iyl · ya"


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


# ── Страницы дат ─────────────────────────────────────────────────────────────

def test_nav_lists_only_working_days(app_session_factory, admin_engine,
                                     clinic_a, doctor_a, service_cleaning):
    make_service(admin_engine, clinic_a, "checkup", 30)  # дефолт без услуги
    monday = next_monday()
    engine, notifier = make(app_session_factory, clinic_a, [],
                            clock=lambda: at_tashkent(monday, "08:00"))
    reply = engine.handle_action(CHAT, f"cal:nav:{monday.isoformat()}")

    assert reply.edit, "страница дат редактирует сообщение"
    days = page_days(reply)
    assert len(days) == 10, "страница из 10 доступных дней"
    assert all(d.weekday() != 6 for d in days), "воскресений нет в списке"
    assert days == sorted(days) and days[0] >= monday
    assert notifier.calls == []


def test_more_dates_paginates_forward_and_back(app_session_factory, admin_engine,
                                               clinic_a, doctor_a, service_cleaning):
    make_service(admin_engine, clinic_a, "checkup", 30)
    monday = next_monday()
    engine, _ = make(app_session_factory, clinic_a, [],
                     clock=lambda: at_tashkent(monday, "08:00"))
    first = engine.handle_action(CHAT, f"cal:nav:{monday.isoformat()}")
    more = [b for b in flat(first.button_rows)
            if b.label == TEMPLATES["btn_more_dates"]["ru"]]
    assert more, "есть «Ещё даты ▶»"

    second = engine.handle_action(CHAT, more[0].action)
    assert min(page_days(second)) > max(page_days(first)), "вторая страница дальше"
    back = [b for b in flat(second.button_rows)
            if b.label == TEMPLATES["btn_first_dates"]["ru"]]
    assert back, "со второй страницы можно вернуться"

    again = engine.handle_action(CHAT, back[0].action)
    assert page_days(again) == page_days(first)


def test_nav_outside_horizon_resets_to_first_page(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    make_service(admin_engine, clinic_a, "checkup", 30)
    monday = next_monday()
    engine, _ = make(app_session_factory, clinic_a, [],
                     clock=lambda: at_tashkent(monday, "08:00"))
    far = monday + timedelta(days=200)
    reply = engine.handle_action(CHAT, f"cal:nav:{far.isoformat()}")
    assert page_days(reply)[0] >= monday, "за горизонтом — первая страница"


def test_legacy_month_nav_still_works(app_session_factory, admin_engine,
                                      clinic_a, doctor_a, service_cleaning):
    # старые сообщения с месячной сеткой (П-5) шлют cal:nav:YYYY-MM
    make_service(admin_engine, clinic_a, "checkup", 30)
    monday = next_monday()
    engine, _ = make(app_session_factory, clinic_a, [],
                     clock=lambda: at_tashkent(monday, "08:00"))
    reply = engine.handle_action(CHAT, f"cal:nav:{monday:%Y-%m}")
    assert page_days(reply), "legacy-формат не падает и даёт страницу дат"


def test_nav_garbage_is_stale(app_session_factory, admin_engine, clinic_a,
                              doctor_a, service_cleaning):
    monday = next_monday()
    engine, _ = make(app_session_factory, clinic_a, [],
                     clock=lambda: at_tashkent(monday, "08:00"))
    reply = engine.handle_action(CHAT, "cal:nav:зюзя")
    assert TEMPLATES["stale_button"]["ru"] in reply.text


def test_noop_and_none_cells_legacy(app_session_factory, admin_engine, clinic_a,
                                    doctor_a, service_cleaning):
    # заглушки старых сообщений с сеткой: молча / toast
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
    assert reply.edit and page_days(reply), "перерисована свежая страница дат"


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


# ── «Нет слотов на 2 недели» → даты/честный текст + FYI раз в день ───────────

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
