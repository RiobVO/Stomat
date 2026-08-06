"""FAQ-темы оплата/телефон + карточка «ℹ️ О клинике» (полировка-2, задача Б).

Зеркало механики адреса (П-2б): поле клиники заполнено — бот отвечает сам
без LLM и без копилки; NULL — штатный путь «не понял» + кнопка call_admin
+ вопрос в копилку. Карточка — новая кнопка главного меню, посреди
сценария работает через _with_reprompt (шаг не сбит).
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from conftest import at_tashkent, next_monday
from navbat.dialog.dialog_common import (
    mentions_payment_question, mentions_phone_question)
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.replies import TEMPLATES, menu_rows
from navbat.nlu.extractor import FakeExtractor
from test_dialog_booking import CHAT, RecordingNotifier, extr
from test_dialog_contact import CountingExtractor
from test_faq_layer import saved_questions


def make(app_session_factory, clinic_id, script, clock=None):
    notifier = RecordingNotifier()
    engine = DialogEngine(app_session_factory, clinic_id,
                          extractor=FakeExtractor(script=script),
                          notifier=notifier, clock=clock)
    return engine, notifier


def set_clinic_field(admin_engine, clinic_id, field, value):
    with admin_engine.begin() as conn:
        conn.execute(text(f"UPDATE clinic SET {field} = :v WHERE id = :c"),
                     {"v": value, "c": clinic_id})


# ── Чистые детекторы: оплата ─────────────────────────────────────────────────

@pytest.mark.parametrize("text_", [
    "рассрочка есть?",
    "можно оплатить картой?",
    "вы принимаете наличные?",
    "какая оплата у вас?",
    "bo'lib to'lasa bo'ladimi?",
    "karta bilan to'lasa bo'ladimi?",
    "naqd olasizmi?",
    "to'lov qanday?",
])
def test_payment_detector_positive(text_):
    assert mentions_payment_question(text_)


@pytest.mark.parametrize("text_", [
    "сколько стоит чистка?",
    "оставил номер соседу",
    "запишите на завтра",
    "болит зуб",
])
def test_payment_detector_negative(text_):
    assert not mentions_payment_question(text_)


# ── Чистые детекторы: телефон ────────────────────────────────────────────────

@pytest.mark.parametrize("text_", [
    "какой у вас номер?",
    "какой у вас телефон?",
    "можно вам позвонить?",
    "не могу дозвониться",
    "номер клиники подскажите",
    "qo'ng'iroq qilsam bo'ladimi?",
    "telefon raqamingiz bormi?",
])
def test_phone_detector_positive(text_):
    assert mentions_phone_question(text_)


@pytest.mark.parametrize("text_", [
    "оставил номер соседу",
    "запишите на чистку",
    "сколько стоит чистка?",
    "рассрочка есть?",
])
def test_phone_detector_negative(text_):
    assert not mentions_phone_question(text_)


# ── FAQ-ответы из полей клиники ──────────────────────────────────────────────

def test_payment_question_answers_when_set(app_session_factory, admin_engine,
                                           clinic_a, doctor_a):
    set_clinic_field(admin_engine, clinic_a, "payment_info",
                     "наличные, карта, рассрочка до 6 мес")
    engine, notifier = make(app_session_factory, clinic_a,
                            [extr(intent="question")])
    reply = engine.handle_text(CHAT, "рассрочка есть?")

    assert "рассрочка до 6 мес" in reply.text
    assert notifier.calls == []
    assert saved_questions(admin_engine) == [], "отвеченный вопрос не копится"


def test_payment_question_without_info_falls_to_not_understood(
        app_session_factory, admin_engine, clinic_a, doctor_a):
    engine, notifier = make(app_session_factory, clinic_a,
                            [extr(intent="question")])
    reply = engine.handle_text(CHAT, "можно оплатить картой?")

    assert [b.action for b in reply.buttons] == ["call_admin"], \
        "оплата не задана — честное «не понял» с кнопкой к человеку"
    assert notifier.calls == []
    assert len(saved_questions(admin_engine)) == 1, "вопрос копится владельцу"


def test_phone_question_answers_when_set(app_session_factory, admin_engine,
                                         clinic_a, doctor_a):
    set_clinic_field(admin_engine, clinic_a, "phone", "+998 71 200-00-00")
    engine, notifier = make(app_session_factory, clinic_a,
                            [extr(intent="question")])
    reply = engine.handle_text(CHAT, "какой у вас номер?")

    assert "200-00-00" in reply.text
    assert notifier.calls == []
    assert saved_questions(admin_engine) == []


def test_phone_question_without_phone_falls_to_not_understood(
        app_session_factory, admin_engine, clinic_a, doctor_a):
    engine, notifier = make(app_session_factory, clinic_a,
                            [extr(intent="question")])
    reply = engine.handle_text(CHAT, "можно вам позвонить?")

    assert [b.action for b in reply.buttons] == ["call_admin"]
    assert notifier.calls == []
    assert len(saved_questions(admin_engine)) == 1


# ── Карточка «ℹ️ О клинике» ──────────────────────────────────────────────────

def about_engine(app_session_factory, clinic_id, script=(), clock=None):
    extractor = CountingExtractor(list(script))
    engine = DialogEngine(app_session_factory, clinic_id, extractor=extractor,
                          notifier=RecordingNotifier(), clock=clock)
    return engine, extractor


def start_with_menu(engine, lang="ru"):
    engine.handle_text(CHAT, "/start")
    engine.handle_action(CHAT, f"lang:{lang}")


def test_about_card_renders_filled_fields(app_session_factory, admin_engine,
                                          clinic_a, doctor_a):
    set_clinic_field(admin_engine, clinic_a, "address", "Ташкент, Навои 10")
    set_clinic_field(admin_engine, clinic_a, "payment_info", "наличные и карта")
    set_clinic_field(admin_engine, clinic_a, "phone", "+998 71 200-00-00")
    engine, extractor = about_engine(
        app_session_factory, clinic_a,
        clock=lambda: at_tashkent(next_monday(), "10:00"))
    start_with_menu(engine)

    reply = engine.handle_text(CHAT, TEMPLATES["btn_menu_about"]["ru"])
    assert "Clinic A" in reply.text, "заголовок — имя клиники"
    assert "09:00" in reply.text and "18:00" in reply.text, "часы есть всегда"
    assert "Навои 10" in reply.text
    assert "наличные и карта" in reply.text
    assert "200-00-00" in reply.text
    assert extractor.calls == [], "кнопка меню не уходит в NLU"


def test_about_card_skips_empty_fields(app_session_factory, admin_engine,
                                       clinic_a, doctor_a):
    # ничего не заполнено: в карточке только заголовок и часы — без
    # пустых строк «адрес/оплата/телефон»
    engine, _ = about_engine(
        app_session_factory, clinic_a,
        clock=lambda: at_tashkent(next_monday(), "10:00"))
    start_with_menu(engine)

    reply = engine.handle_text(CHAT, TEMPLATES["btn_menu_about"]["ru"])
    assert "Clinic A" in reply.text
    assert "09:00" in reply.text
    assert "📍" not in reply.text
    assert "💳" not in reply.text
    assert "📞" not in reply.text


def test_about_card_uz_label_matches_too(app_session_factory, admin_engine,
                                         clinic_a, doctor_a):
    engine, extractor = about_engine(
        app_session_factory, clinic_a,
        clock=lambda: at_tashkent(next_monday(), "10:00"))
    start_with_menu(engine, lang="uz")

    reply = engine.handle_text(CHAT, TEMPLATES["btn_menu_about"]["uz"])
    assert "Clinic A" in reply.text
    assert extractor.calls == []


def test_about_card_mid_booking_reprompts_step(app_session_factory, admin_engine,
                                               clinic_a, doctor_a,
                                               service_cleaning):
    from test_dialog_booking import fsm_state
    engine, _ = about_engine(
        app_session_factory, clinic_a, script=[extr(service="cleaning")],
        clock=lambda: at_tashkent(next_monday(), "10:00"))
    engine.handle_text(CHAT, "хочу чистку")          # booking_collect

    reply = engine.handle_text(CHAT, TEMPLATES["btn_menu_about"]["ru"])
    assert "Clinic A" in reply.text
    assert TEMPLATES["ask_date"]["ru"] in reply.text, "шаг повторён"
    assert fsm_state(admin_engine) == "booking_collect"


# ── Меню: 6 кнопок ───────────────────────────────────────────────────────────

def test_menu_rows_include_about_button():
    assert menu_rows("ru") == (
        (TEMPLATES["btn_menu_book"]["ru"],),
        (TEMPLATES["btn_menu_resched"]["ru"], TEMPLATES["btn_menu_cancel"]["ru"]),
        (TEMPLATES["btn_menu_prices"]["ru"], TEMPLATES["btn_menu_about"]["ru"]),
        (TEMPLATES["btn_menu_lang"]["ru"],),
    )


# ── Онбординг ────────────────────────────────────────────────────────────────

def clinic_field(admin_engine, clinic_id, field):
    with admin_engine.begin() as conn:
        return conn.execute(
            text(f"SELECT {field} FROM clinic WHERE id = :c"),
            {"c": clinic_id}).scalar_one()


def test_onboard_sets_payment_and_phone(app_session_factory, admin_engine,
                                        clinic_a):
    from navbat.onboard import set_clinic_payment, set_clinic_phone
    set_clinic_payment(app_session_factory, clinic_a, "наличные, карта")
    set_clinic_phone(app_session_factory, clinic_a, "+998 71 200-00-00")
    assert clinic_field(admin_engine, clinic_a, "payment_info") == \
        "наличные, карта"
    assert clinic_field(admin_engine, clinic_a, "phone") == "+998 71 200-00-00"


def test_onboard_empty_value_clears_field(app_session_factory, admin_engine,
                                          clinic_a):
    from navbat.onboard import set_clinic_payment, set_clinic_phone
    set_clinic_payment(app_session_factory, clinic_a, "карта")
    set_clinic_payment(app_session_factory, clinic_a, "")
    set_clinic_phone(app_session_factory, clinic_a, "+998 71 1")
    set_clinic_phone(app_session_factory, clinic_a, "")
    assert clinic_field(admin_engine, clinic_a, "payment_info") is None
    assert clinic_field(admin_engine, clinic_a, "phone") is None
