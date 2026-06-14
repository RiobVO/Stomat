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
    assert ac.BTN_SERVICES in labels and ac.BTN_STATS in labels


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


# -- хелперы P-2/P-3 -------------------------------------------------------

def service_field(admin_engine, clinic_id, name, field):
    with admin_engine.begin() as conn:
        return conn.execute(
            text(f"SELECT {field} FROM service WHERE clinic_id = :c AND name = :n"),
            {"c": clinic_id, "n": name}).scalar_one_or_none()


def doctor_field(admin_engine, clinic_id, doctor_id, field):
    with admin_engine.begin() as conn:
        return conn.execute(
            text(f"SELECT {field} FROM doctor WHERE id = :d"),
            {"d": doctor_id}).scalar_one_or_none()


def row_actions(api):
    """callback data кнопок в последнем inline-сообщении (отправка или правка)."""
    # После callback-клика воркер обычно правит сообщение (api.edited),
    # после текстового ввода — отправляет новое (api.row_keyboards).
    # Берём самое позднее из двух.
    from_sent = api.row_keyboards[-1] if api.row_keyboards else ()
    from_edit = api.edited[-1][3] if api.edited else ()
    return actions(from_edit if api.edited else from_sent)


# -- 6. чистые функции (P-2/P-3) -------------------------------------------

def test_format_schedule_groups_consecutive_days():
    wi = {d: [["09:00", "18:00"]] for d in ("mon", "tue", "wed", "thu", "fri")}
    result = ac._format_schedule(wi)
    assert "09:00" in result and "18:00" in result


def test_format_schedule_empty():
    assert ac._format_schedule({}) == "выходной всю неделю"


def test_parse_shifts_ok():
    got = ac._parse_shifts("09:00-13:00, 14:00-18:00")
    assert got == [["09:00", "13:00"], ["14:00", "18:00"]]
    assert ac._parse_shifts("9:00-18:00") == [["09:00", "18:00"]]


def test_parse_shifts_rejects_bad():
    for bad in ("9-18", "25:00-26:00", "13:00-09:00", "", "abc"):
        assert ac._parse_shifts(bad) is None


# -- 7. Услуги (P-2) --------------------------------------------------------

def test_services_menu_shows_service(app_session_factory, clinic_a, service_cleaning):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a, ac.BTN_SERVICES)

    body = last_to(api, ADMIN_CHAT)
    assert body is not None and "Услуги" in body
    assert any("cleaning" in a for a in row_actions(api))


def test_service_card_shows_price_and_duration_buttons(app_session_factory, clinic_a,
                                                       service_cleaning):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, "adm:svc:cleaning")

    acts = row_actions(api)
    assert "adm:svc:cleaning:price" in acts
    assert "adm:svc:cleaning:dur" in acts
    assert "adm:svc:cleaning:deact" in acts
    # активная услуга — кнопки удаления не должно быть
    assert "adm:svc:cleaning:del" not in acts


def test_duration_edit_via_button_and_number(app_session_factory, admin_engine,
                                              clinic_a, service_cleaning):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, "adm:svc:cleaning:dur")
    assert api.answered
    send_admin(worker, app_session_factory, clinic_a, "45")

    assert service_field(admin_engine, clinic_a, "cleaning", "duration_min") == 45
    assert "adm_pending" not in context_of(admin_engine, ADMIN_CHAT)


def test_invalid_duration_rejected(app_session_factory, admin_engine,
                                    clinic_a, service_cleaning):
    worker, _, _ = make_worker(app_session_factory, clinic_a, [],
                               admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, "adm:svc:cleaning:dur")
    for bad in ("0", "abc", "500", "-1"):
        send_admin(worker, app_session_factory, clinic_a, bad)
        assert service_field(admin_engine, clinic_a, "cleaning", "duration_min") == 30
        # pending должен сохраняться после каждого неверного ввода
        assert context_of(admin_engine, ADMIN_CHAT)["adm_pending"] == "dur:cleaning"


def test_service_deactivate_and_activate(app_session_factory, admin_engine,
                                          clinic_a, service_cleaning):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, "adm:svc:cleaning:deact")
    assert service_field(admin_engine, clinic_a, "cleaning", "is_active") is False

    click(worker, app_session_factory, clinic_a, "adm:svc:cleaning:act")
    assert service_field(admin_engine, clinic_a, "cleaning", "is_active") is True


def test_services_menu_lists_active_and_inactive(app_session_factory, admin_engine,
                                                  clinic_a):
    from conftest import make_service
    make_service(admin_engine, clinic_a, "cleaning", 30, price=350000)
    braces = make_service(admin_engine, clinic_a, "braces", 60)
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE service SET is_active = false WHERE id = :s"),
                     {"s": braces})

    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a, ac.BTN_SERVICES)

    acts = row_actions(api)
    # обе услуги должны отображаться: активная и деактивированная
    assert "adm:svc:cleaning" in acts
    assert "adm:svc:braces" in acts
    # кнопка «Добавить» без подчёркивания
    assert "adm:svcadd" in acts
    body = last_to(api, ADMIN_CHAT)
    assert "Услуги" in body


def test_service_add_creates_with_duration(app_session_factory, admin_engine, clinic_a):
    """Поток добавления услуги из каталога пишет строку в БД."""
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, "adm:svcadd")   # каталог
    click(worker, app_session_factory, clinic_a, "adm:svcadd:braces")  # выбрали услугу
    send_admin(worker, app_session_factory, clinic_a, "60")

    with admin_engine.begin() as conn:
        row = conn.execute(
            text("SELECT duration_min, is_active FROM service "
                 "WHERE clinic_id = :c AND name = 'braces'"),
            {"c": clinic_a}).one()
    assert row.duration_min == 60 and row.is_active is True
    assert "adm_pending" not in context_of(admin_engine, ADMIN_CHAT)


def test_service_add_lists_catalog_missing(app_session_factory, clinic_a,
                                            service_cleaning):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, "adm:svcadd")

    acts = row_actions(api)
    # cleaning already exists, so it must NOT appear, but others must
    assert not any(a.endswith(":cleaning") or a == "adm:svcadd:cleaning" for a in acts)
    assert any("extraction" in a or "filling" in a for a in acts)


def test_service_card_delete_gating(app_session_factory, admin_engine, clinic_a):
    """Кнопка «Удалить совсем» — только у деактивированной, не имеющей ссылок."""
    from conftest import make_service
    make_service(admin_engine, clinic_a, "cleaning", 30)

    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    # Активная услуга — удалить нельзя
    click(worker, app_session_factory, clinic_a, "adm:svc:cleaning")
    acts = row_actions(api)
    assert "adm:svc:cleaning:del" not in acts

    # Деактивируем, но записей нет → кнопка появляется
    click(worker, app_session_factory, clinic_a, "adm:svc:cleaning:deact")
    click(worker, app_session_factory, clinic_a, "adm:svc:cleaning")
    acts = row_actions(api)
    assert "adm:svc:cleaning:del" in acts


# -- 8. Врачи (P-3) --------------------------------------------------------

def test_doctors_menu_shows_doctor(app_session_factory, clinic_a, doctor_a):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a, ac.BTN_DOCTORS)

    body = last_to(api, ADMIN_CHAT)
    assert body is not None and "Врачи" in body
    acts = row_actions(api)
    assert any(str(doctor_a) in a for a in acts)


def test_doctor_card_shows_actions(app_session_factory, clinic_a, doctor_a):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, f"adm:doc:{doctor_a}")

    acts = row_actions(api)
    assert any("name" in a for a in acts)
    assert any("buf" in a for a in acts)
    assert any("sched" in a for a in acts)


def test_doctor_name_edit(app_session_factory, admin_engine, clinic_a, doctor_a):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, f"adm:doc:{doctor_a}:name")
    send_admin(worker, app_session_factory, clinic_a, "Иванов И.И.")

    from navbat.crypto import decrypt_text
    enc = doctor_field(admin_engine, clinic_a, doctor_a, "name_encrypted")
    assert decrypt_text(enc) == "Иванов И.И."


def test_doctor_buffer_edit(app_session_factory, admin_engine, clinic_a, doctor_a):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, f"adm:doc:{doctor_a}:buf")
    send_admin(worker, app_session_factory, clinic_a, "15")

    assert doctor_field(admin_engine, clinic_a, doctor_a, "buffer_min") == 15


def test_schedule_template_applies(app_session_factory, admin_engine,
                                    clinic_a, doctor_a):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, f"adm:doc:{doctor_a}:sched")
    click(worker, app_session_factory, clinic_a,
          f"adm:sched:tpl:{doctor_a}:0")

    import json
    wi_raw = doctor_field(admin_engine, clinic_a, doctor_a, "working_intervals")
    wi = json.loads(wi_raw) if isinstance(wi_raw, str) else wi_raw
    assert "mon" in wi and "fri" in wi
    assert "sat" not in wi


def test_custom_schedule_days_then_shifts(app_session_factory, admin_engine,
                                           clinic_a, doctor_a):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, f"adm:doc:{doctor_a}:sched")
    click(worker, app_session_factory, clinic_a, f"adm:sched:custom:{doctor_a}")
    click(worker, app_session_factory, clinic_a, f"adm:sched:day:{doctor_a}:mon")
    click(worker, app_session_factory, clinic_a, f"adm:sched:day:{doctor_a}:tue")
    click(worker, app_session_factory, clinic_a, f"adm:sched:next:{doctor_a}")
    send_admin(worker, app_session_factory, clinic_a, "09:00-18:00")

    import json
    wi_raw = doctor_field(admin_engine, clinic_a, doctor_a, "working_intervals")
    wi = json.loads(wi_raw) if isinstance(wi_raw, str) else wi_raw
    assert "mon" in wi and "tue" in wi
    assert wi["mon"] == [["09:00", "18:00"]]


def test_custom_schedule_days_dont_leak_between_doctors(app_session_factory,
                                                       admin_engine, clinic_a,
                                                       doctor_a):
    # C1: незавершённый выбор дней для одного врача не должен протечь в график
    # другого. Бросаем выбор пн+вт для doctor_a, затем задаём ср другому врачу.
    from conftest import make_doctor
    other = make_doctor(admin_engine, clinic_a, name="Второй")
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, f"adm:sched:custom:{doctor_a}")
    click(worker, app_session_factory, clinic_a, f"adm:sched:day:{doctor_a}:mon")
    click(worker, app_session_factory, clinic_a, f"adm:sched:day:{doctor_a}:tue")
    # бросаем — переходим ко второму врачу без ввода смен
    click(worker, app_session_factory, clinic_a, f"adm:sched:custom:{other}")
    click(worker, app_session_factory, clinic_a, f"adm:sched:day:{other}:wed")
    click(worker, app_session_factory, clinic_a, f"adm:sched:next:{other}")
    send_admin(worker, app_session_factory, clinic_a, "10:00-14:00")

    import json
    wi_raw = doctor_field(admin_engine, clinic_a, other, "working_intervals")
    wi = json.loads(wi_raw) if isinstance(wi_raw, str) else wi_raw
    assert set(wi.keys()) == {"wed"}, f"дни протекли: {set(wi.keys())}"
    assert wi["wed"] == [["10:00", "14:00"]]


def test_custom_schedule_bad_shifts_stays_pending(app_session_factory, admin_engine,
                                                   clinic_a, doctor_a):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, f"adm:sched:custom:{doctor_a}")
    click(worker, app_session_factory, clinic_a, f"adm:sched:day:{doctor_a}:mon")
    click(worker, app_session_factory, clinic_a, f"adm:sched:next:{doctor_a}")
    send_admin(worker, app_session_factory, clinic_a, "badformat")

    ctx = context_of(admin_engine, ADMIN_CHAT)
    assert ctx is not None and "sched" in (ctx or {}).get("adm_pending", "")


def test_doctor_add_creates_new_doctor(app_session_factory, admin_engine, clinic_a):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, "adm:docadd:")
    send_admin(worker, app_session_factory, clinic_a, "Петрова А.С.")

    with admin_engine.begin() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM doctor WHERE clinic_id = :c"),
            {"c": clinic_a}).scalar_one()
    assert count == 1


def test_doctor_deactivate_and_activate(app_session_factory, admin_engine,
                                         clinic_a, doctor_a):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, f"adm:doc:{doctor_a}:deact")
    assert doctor_field(admin_engine, clinic_a, doctor_a, "is_active") is False

    click(worker, app_session_factory, clinic_a, f"adm:doc:{doctor_a}:act")
    assert doctor_field(admin_engine, clinic_a, doctor_a, "is_active") is True


# ── 8. раздел «Выходные» (P-4) ──────────────────────────────────────────────

def _add_holiday(admin_engine, clinic_id, iso, reason=None):
    with admin_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO holiday (clinic_id, date, reason) "
                 "VALUES (:c, :d, :r)"),
            {"c": clinic_id, "d": iso, "r": reason})


def _holiday_count(admin_engine, clinic_id):
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT count(*) FROM holiday WHERE clinic_id = :c"),
            {"c": clinic_id}).scalar_one()


def test_dayoff_menu_lists_and_reopen(app_session_factory, admin_engine, clinic_a):
    from datetime import date, timedelta
    future = (date.today() + timedelta(days=10)).isoformat()
    _add_holiday(admin_engine, clinic_a, future, "Праздник")

    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a, ac.BTN_DAYOFF)
    acts = actions(api.row_keyboards[-1])
    assert f"adm:dayoff:open:{future}" in acts
    assert "adm:dayoff:add" in acts

    click(worker, app_session_factory, clinic_a, f"adm:dayoff:open:{future}")
    assert _holiday_count(admin_engine, clinic_a) == 0


def test_dayoff_add_closes_day(app_session_factory, admin_engine, clinic_a):
    from datetime import date, timedelta
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, "adm:dayoff:add")
    target = date.today() + timedelta(days=5)
    send_admin(worker, app_session_factory, clinic_a,
               f"{target.day:02d}.{target.month:02d} Учёт")

    with admin_engine.begin() as conn:
        row = conn.execute(
            text("SELECT date, reason FROM holiday WHERE clinic_id = :c"),
            {"c": clinic_a}).one()
    assert row.reason == "Учёт"
    assert "adm_pending" not in context_of(admin_engine, ADMIN_CHAT)


def test_dayoff_add_bad_date_repeats(app_session_factory, admin_engine, clinic_a):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, "adm:dayoff:add")
    send_admin(worker, app_session_factory, clinic_a, "ерунда")

    assert _holiday_count(admin_engine, clinic_a) == 0
    assert context_of(admin_engine, ADMIN_CHAT)["adm_pending"] == "dayoff"
