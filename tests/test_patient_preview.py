"""Превью «глазами пациента» в админ-чате (карта готовности, №14).

Админ-чат — чистая консоль: пациентский диалог из него недоступен, и на
встрече владелец, взявший свой телефон «дайте попробую», попадал в
админ-меню. Нужен второй аккаунт и объяснение, почему так.

Превью показывает те же тексты, что увидит пациент, прямо в админ-чате —
и после каждой правки цен или адреса владелец видит результат своими
глазами. Ограничение: превью не трогает ни клавиатуру админ-чата, ни
данные — это картинка, а не диалог.
"""
from __future__ import annotations

from sqlalchemy import text

from navbat.telegram import admin_texts as at
from test_admin_console import (ADMIN_CHAT, click, flat, last_menu, last_to,
                                row_actions, send_admin)
from test_tg_worker import make_worker


def _clinic_faq(admin_engine, clinic_id):
    with admin_engine.begin() as conn:
        conn.execute(text(
            "UPDATE service SET price = 350000 WHERE clinic_id = :c "
            "AND name = 'cleaning'"), {"c": clinic_id})
        conn.execute(text(
            "UPDATE clinic SET address = :a, payment_info = :p, phone = :ph "
            "WHERE id = :c"),
            {"a": "Ташкент, ул. Навои, 10", "p": "Наличные, карта, Payme",
             "ph": "+998 71 200-00-00", "c": clinic_id})


def test_preview_button_in_main_menu(app_session_factory, clinic_a):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a, "/start")

    assert at.TEMPLATES["btn_preview"]["ru"] in flat(last_menu(api))


def test_preview_shows_greeting_prices_and_clinic_card(
        app_session_factory, admin_engine, clinic_a, service_cleaning):
    _clinic_faq(admin_engine, clinic_a)
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a,
               at.TEMPLATES["btn_preview"]["ru"])

    body = last_to(api, ADMIN_CHAT)
    assert "Навои" in body, f"адрес пациенту не показан: {body}"
    assert "350 000" in body, "цена услуги не показана"
    assert "Payme" in body


def test_preview_does_not_replace_admin_keyboard(app_session_factory, clinic_a,
                                                 service_cleaning):
    """Пациентское меню — reply-клавиатура. Показать её в админ-чате значит
    затереть админскую: превью рисует кнопки пациента текстом."""
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a, "/start")
    send_admin(worker, app_session_factory, clinic_a,
               at.TEMPLATES["btn_preview"]["ru"])

    # без reply_markup Telegram оставляет прежнюю клавиатуру на экране
    assert last_menu(api) is None, "превью подменило клавиатуру админ-чата"
    body = last_to(api, ADMIN_CHAT)
    assert "Кнопки пациента" in body, "меню пациента должно быть текстом"


def test_preview_can_be_switched_to_uzbek(app_session_factory, admin_engine,
                                          clinic_a, service_cleaning):
    """Владелец обязан увидеть, что именно читает узбекоязычный пациент."""
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a,
               at.TEMPLATES["btn_preview"]["ru"])
    assert "adm:preview:uz" in row_actions(api)

    click(worker, app_session_factory, clinic_a, "adm:preview:uz")
    body = api.edited[-1][2] if api.edited else last_to(api, ADMIN_CHAT)
    assert "Tish tozalash" in body, f"превью не на узбекском: {body}"


def test_preview_creates_no_patient_state(app_session_factory, admin_engine,
                                          clinic_a, service_cleaning):
    """Превью — картинка: ни записей, ни пациентского диалога, ни очереди."""
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a,
               at.TEMPLATES["btn_preview"]["ru"])
    click(worker, app_session_factory, clinic_a, "adm:preview:uz")

    with admin_engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT (SELECT count(*) FROM appointment) "
            "+ (SELECT count(*) FROM waitlist) "
            "+ (SELECT count(*) FROM conversation WHERE tg_chat_id <> :admin)"),
            {"admin": ADMIN_CHAT}).scalar_one()
    assert rows == 0


def test_preview_language_does_not_leak_into_console(app_session_factory,
                                                     clinic_a,
                                                     service_cleaning):
    """Язык превью — про пациента; консоль остаётся на языке владельца."""
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    send_admin(worker, app_session_factory, clinic_a,
               at.TEMPLATES["btn_preview"]["ru"])
    click(worker, app_session_factory, clinic_a, "adm:preview:uz")
    send_admin(worker, app_session_factory, clinic_a, "/start")

    assert at.TEMPLATES["btn_services"]["ru"] in flat(last_menu(api))
