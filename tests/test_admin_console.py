"""Админ-консоль на кнопках (инкремент 1): владелец правит цены и FAQ-поля
из админ-чата, без CLI и без слэш-команд.

Решение пользователя: админ-чат — ЧИСТАЯ консоль (пациентский диалог из него
недоступен). Объём инкремента: каркас меню + цены + FAQ + переиспользование
/stats и паузы как кнопок.
"""
from __future__ import annotations

from sqlalchemy import text

from navbat.telegram import admin_console as ac
from test_dialog_booking import CHAT
from test_tg_worker import context_of, make_worker, put_callback, put_message

ADMIN_CHAT = 777


# ── хелперы ───────────────────────────────────────────────────────────────

def send_admin(worker, sf, clinic, text_in, chat_id=ADMIN_CHAT):
    put_message(sf, clinic, text_in, chat_id=chat_id)
    worker.process_one()


def click(worker, sf, clinic, data, chat_id=ADMIN_CHAT):
    put_callback(sf, clinic, data, chat_id=chat_id)
    worker.process_one()


def last_to(api, chat_id):
    msgs = [t for c, t, _ in api.sent if c == chat_id]
    return msgs[-1] if msgs else None


def last_menu(api):
    """Ряды reply-клавиатуры последней отправки (или None)."""
    return api.keyboards[-1][1]


def flat(rows):
    return [x for row in (rows or ()) for x in row]


def actions(rows):
    return [b.action for row in (rows or ()) for b in row]


def price_in_db(admin_engine, clinic_id, name="cleaning"):
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT price FROM service WHERE clinic_id = :c AND name = :n"),
            {"c": clinic_id, "n": name}).scalar_one()


def clinic_field(admin_engine, clinic_id, field):
    with admin_engine.begin() as conn:
        return conn.execute(
            text(f"SELECT {field} FROM clinic WHERE id = :c"),
            {"c": clinic_id}).scalar_one()


def set_paused(admin_engine, clinic_id, value):
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE clinic SET bot_paused = :v WHERE id = :c"),
                     {"v": value, "c": clinic_id})


# ── 1. каркас и авторизация ────────────────────────────────────────────────

def test_admin_start_shows_admin_console(app_session_factory, clinic_a):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a, "/start")

    body = last_to(api, ADMIN_CHAT)
    assert "Админ-консоль" in body
    labels = flat(last_menu(api))
    assert ac.BTN_PRICES in labels and ac.BTN_STATS in labels


def test_patient_start_unaffected(app_session_factory, clinic_a):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    put_message(app_session_factory, clinic_a, "/start", chat_id=CHAT)
    worker.process_one()

    body = last_to(api, CHAT)
    assert body is not None and "Админ-консоль" not in body


def test_non_admin_cannot_change_price(app_session_factory, admin_engine,
                                       clinic_a, service_cleaning):
    # пациент шлёт adm:-callback и число — авторизация по chat_id не пускает
    worker, _, _ = make_worker(app_session_factory, clinic_a, [],
                               admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, "adm:price:cleaning",
          chat_id=CHAT)
    send_admin(worker, app_session_factory, clinic_a, "400000", chat_id=CHAT)

    assert price_in_db(admin_engine, clinic_a) is None


# ── 2. цены ─────────────────────────────────────────────────────────────────

def test_price_edit_via_button_and_number(app_session_factory, admin_engine,
                                          clinic_a, service_cleaning):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, "adm:price:cleaning")
    assert api.answered, "callback подтверждён"
    send_admin(worker, app_session_factory, clinic_a, "400000")

    assert price_in_db(admin_engine, clinic_a) == 400000
    assert "adm_pending" not in context_of(admin_engine, ADMIN_CHAT)
    body = last_to(api, ADMIN_CHAT)
    assert "✅" in body and "400 000" in body


def test_invalid_price_rejected_without_write(app_session_factory, admin_engine,
                                              clinic_a, service_cleaning):
    worker, _, _ = make_worker(app_session_factory, clinic_a, [],
                               admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, "adm:price:cleaning")
    for bad in ("abc", "-5", "0", "12.5"):
        send_admin(worker, app_session_factory, clinic_a, bad)
        assert price_in_db(admin_engine, clinic_a) is None
        assert context_of(admin_engine, ADMIN_CHAT)["adm_pending"] == "price:cleaning"


# ── 3. отмена и приоритеты ──────────────────────────────────────────────────

def test_cancel_clears_pending(app_session_factory, admin_engine, clinic_a,
                               service_cleaning):
    worker, _, _ = make_worker(app_session_factory, clinic_a, [],
                               admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, "adm:price:cleaning")
    click(worker, app_session_factory, clinic_a, "adm:cancel")
    assert "adm_pending" not in context_of(admin_engine, ADMIN_CHAT)

    # следующее число — НЕ цена (pending снят)
    send_admin(worker, app_session_factory, clinic_a, "400000")
    assert price_in_db(admin_engine, clinic_a) is None


def test_slash_overrides_pending(app_session_factory, admin_engine, clinic_a,
                                 service_cleaning):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, "adm:price:cleaning")
    # /resume перехватывается слэш-веткой воркера ДО консоли — не как цена
    send_admin(worker, app_session_factory, clinic_a, "/resume")

    assert price_in_db(admin_engine, clinic_a) is None
    assert "снова принимает" in last_to(api, ADMIN_CHAT)


def test_menu_label_during_pending_exits_input(app_session_factory, admin_engine,
                                               clinic_a, service_cleaning):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, "adm:price:cleaning")
    send_admin(worker, app_session_factory, clinic_a, ac.BTN_ABOUT)

    assert "adm_pending" not in context_of(admin_engine, ADMIN_CHAT)
    assert "О клинике" in last_to(api, ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a, "400000")
    assert price_in_db(admin_engine, clinic_a) is None


# ── 4. FAQ ──────────────────────────────────────────────────────────────────

def test_faq_address_via_button_and_text(app_session_factory, admin_engine,
                                          clinic_a):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, "adm:faq:address")
    send_admin(worker, app_session_factory, clinic_a,
               "ул. Навои, 12 & корпус Б")

    assert clinic_field(admin_engine, clinic_a, "address") == \
        "ул. Навои, 12 & корпус Б"
    assert "adm_pending" not in context_of(admin_engine, ADMIN_CHAT)

    # повторный вход показывает текущее значение в HTML-теле с экранированием «&»
    click(worker, app_session_factory, clinic_a, "adm:faq:address")
    edited_text = api.edited[-1][2]
    assert "&amp;" in edited_text


def test_empty_faq_rejected(app_session_factory, admin_engine, clinic_a):
    worker, _, _ = make_worker(app_session_factory, clinic_a, [],
                               admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, "adm:faq:phone")
    send_admin(worker, app_session_factory, clinic_a, "   ")

    assert clinic_field(admin_engine, clinic_a, "phone") is None
    assert context_of(admin_engine, ADMIN_CHAT)["adm_pending"] == "faq:phone"


# ── 5. статистика и пауза ───────────────────────────────────────────────────

def test_stats_button_reuses_stats_reply(app_session_factory, clinic_a):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a, ac.BTN_STATS)

    assert any(a.startswith("stats:") for a in actions(api.row_keyboards[-1]))


def test_pause_toggle(app_session_factory, admin_engine, clinic_a):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a, ac.BTN_PAUSE)
    assert clinic_field(admin_engine, clinic_a, "bot_paused") is True
    assert ac.BTN_RESUME in flat(last_menu(api))

    send_admin(worker, app_session_factory, clinic_a, ac.BTN_RESUME)
    assert clinic_field(admin_engine, clinic_a, "bot_paused") is False
    assert ac.BTN_PAUSE in flat(last_menu(api))


def test_admin_unknown_callback_stays_in_console(app_session_factory, clinic_a):
    # старая пациентская кнопка (a:N) в админ-чате НЕ уходит в пациентский
    # диалог — админ-чат остаётся чистой консолью и для callback'ов
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, "a:1")

    assert api.answered, "callback подтверждён"
    assert "Админ-консоль" in last_to(api, ADMIN_CHAT)


def test_console_alive_while_paused(app_session_factory, admin_engine, clinic_a,
                                    service_cleaning):
    set_paused(admin_engine, clinic_a, True)
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    # консоль работает на паузе (как /stats, конвенция C-4)
    click(worker, app_session_factory, clinic_a, "adm:price:cleaning")
    send_admin(worker, app_session_factory, clinic_a, "400000")
    assert price_in_db(admin_engine, clinic_a) == 400000

    # пациент на паузе получает вежливый ответ, не диалог
    put_message(app_session_factory, clinic_a, "привет", chat_id=CHAT)
    worker.process_one()
    assert last_to(api, CHAT) is not None
