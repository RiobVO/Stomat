"""FAQ-слой без LLM (П-2б): часы работы и адрес — бот отвечает сам.

Неотвеченные вопросы копятся в unanswered_question (анонимно, телефоны
маскируются) и приходят владельцу в вечернем дайджесте — данные о спросе
без дёрганья днём.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from conftest import at_tashkent, next_monday, next_sunday
from navbat.dialog.dialog_common import (
    mentions_address_question, mentions_hours_question)
from navbat.dialog.fsm import DialogEngine
from navbat.nlu.extractor import FakeExtractor
from navbat.nlu.wrappers import redact_phones
from navbat.reminders import ReminderService
from test_dialog_booking import (
    CHAT, RecordingNotifier, explicit, extr, slot_buttons)
from test_tg_worker import FakeTelegramAPI

TASHKENT = ZoneInfo("Asia/Tashkent")
ADMIN_CHAT = 900


def make(app_session_factory, clinic_id, script, clock=None):
    notifier = RecordingNotifier()
    engine = DialogEngine(app_session_factory, clinic_id,
                          extractor=FakeExtractor(script=script),
                          notifier=notifier, clock=clock)
    return engine, notifier


def saved_questions(admin_engine) -> list[str]:
    with admin_engine.begin() as conn:
        return list(conn.execute(
            text("SELECT question FROM unanswered_question ORDER BY id")
        ).scalars())


# ── Чистые детекторы ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("text_", [
    "во сколько вы работаете?",
    "до скольки работаете сегодня?",
    "со скольки открываетесь?",
    "какой у вас график?",
    "режим работы подскажите",
    "когда вы открываетесь?",
    "ish vaqti qanday?",
    "qachongacha ishlaysizlar?",
])
def test_hours_detector_positive(text_):
    assert mentions_hours_question(text_)


@pytest.mark.parametrize("text_", [
    "запишите на чистку",
    "сколько стоит отбеливание?",
    "болит зуб",
    "а ещё слоты есть?",
])
def test_hours_detector_negative(text_):
    assert not mentions_hours_question(text_)


@pytest.mark.parametrize("text_", [
    "какой у вас адрес?",
    "где вы находитесь?",
    "как до вас добраться?",
    "куда подойти?",
    "manzilingiz qayerda?",
    "qanday boraman?",
])
def test_address_detector_positive(text_):
    assert mentions_address_question(text_)


@pytest.mark.parametrize("text_", [
    "запишите на завтра",
    "перенесите запись",
    "сколько стоит чистка?",
])
def test_address_detector_negative(text_):
    assert not mentions_address_question(text_)


def test_redact_phones_masks_numbers():
    masked = redact_phones("перезвоните мне на +998 90 123-45-67 пожалуйста")
    assert "998" not in masked and "[phone]" in masked


# ── Часы работы ──────────────────────────────────────────────────────────────

def test_hours_question_answers_with_today_window(
        app_session_factory, admin_engine, clinic_a, doctor_a):
    # график conftest: 09:00–13:00 и 14:00–18:00 → окно дня 09:00–18:00
    engine, notifier = make(app_session_factory, clinic_a,
                            [extr(intent="question")],
                            clock=lambda: at_tashkent(next_monday(), "10:00"))
    reply = engine.handle_text(CHAT, "до скольки вы сегодня работаете?")

    assert "09:00" in reply.text and "18:00" in reply.text
    assert notifier.calls == []
    assert saved_questions(admin_engine) == [], "отвеченный вопрос не копится"


def test_hours_question_on_closed_day_points_to_next_working(
        app_session_factory, admin_engine, clinic_a, doctor_a):
    # воскресенья нет в графике → ближайший рабочий день — понедельник
    sunday = next_sunday()
    monday = sunday + timedelta(days=1)
    engine, _ = make(app_session_factory, clinic_a,
                     [extr(intent="question")],
                     clock=lambda: at_tashkent(sunday, "12:00"))
    reply = engine.handle_text(CHAT, "во сколько вы работаете?")

    assert f"{monday:%d.%m}" in reply.text
    assert "09:00" in reply.text and "18:00" in reply.text


def test_hours_beats_availability_marker(app_session_factory, admin_engine,
                                         clinic_a, doctor_a):
    # «ish vaqti» содержит маркер наличия «vaqt» — FAQ должен победить:
    # пациент спросил про часы, а не про слоты
    engine, _ = make(app_session_factory, clinic_a,
                     [extr(intent="question", language="uz")],
                     clock=lambda: at_tashkent(next_monday(), "10:00"))
    reply = engine.handle_text(CHAT, "ish vaqti qanday?")

    assert "09:00" in reply.text
    assert not any(b.action.startswith("date:") for b in reply.buttons)


def test_hours_mid_booking_reprompts_slots(
        app_session_factory, admin_engine, clinic_a, doctor_a, service_cleaning):
    day = next_monday()
    engine, notifier = make(
        app_session_factory, clinic_a,
        [extr(service="cleaning", date_ref=explicit(day)),
         extr(intent="question")],
        clock=lambda: at_tashkent(day, "08:00"))
    engine.handle_text(CHAT, "чистка в понедельник")

    reply = engine.handle_text(CHAT, "а до скольки вы работаете?")
    assert "18:00" in reply.text
    assert slot_buttons(reply), "вопрос вбок не сбил шаг — слоты повторены"
    assert notifier.calls == []


# ── FAQ до LLM (живая находка 12.06: 55/55 фраз батареи дёргали модель) ─────

class ForbiddenExtractor:
    """LLM не должен вызываться: FAQ при известном языке — ноль токенов."""

    def extract(self, text):  # noqa: ANN001
        raise AssertionError(f"LLM вызван для FAQ-фразы: {text!r}")


def make_lang_known(app_session_factory, clinic_id, clock=None):
    notifier = RecordingNotifier()
    engine = DialogEngine(app_session_factory, clinic_id,
                          extractor=ForbiddenExtractor(),
                          notifier=notifier, clock=clock)
    engine.handle_text(CHAT, "/start")
    engine.handle_action(CHAT, "lang:ru")  # язык выбран кнопкой — как в проде
    return engine, notifier


def test_faq_hours_answers_without_llm(app_session_factory, admin_engine,
                                       clinic_a, doctor_a):
    engine, notifier = make_lang_known(
        app_session_factory, clinic_a,
        clock=lambda: at_tashkent(next_monday(), "10:00"))
    reply = engine.handle_text(CHAT, "до скольки вы работаете?")

    assert "09:00" in reply.text and "18:00" in reply.text
    assert notifier.calls == []


def test_faq_hours_with_today_not_hijacked_by_backstop(
        app_session_factory, admin_engine, clinic_a, doctor_a):
    # «сегодня» давало date_ref=today → booking_like бэкстоп показывал слоты
    # ВМЕСТО часов (живой транскрипт S06); до LLM перехват — часы побеждают
    engine, _ = make_lang_known(
        app_session_factory, clinic_a,
        clock=lambda: at_tashkent(next_monday(), "10:00"))
    reply = engine.handle_text(CHAT, "до скольки работаете сегодня?")

    assert "09:00" in reply.text and "18:00" in reply.text
    assert not any(b.action.startswith("slot:") for b in reply.buttons)


def test_faq_address_answers_without_llm(app_session_factory, admin_engine,
                                         clinic_a, doctor_a):
    set_address(admin_engine, clinic_a, "Ташкент, ул. Навои, 10")
    engine, _ = make_lang_known(app_session_factory, clinic_a)
    reply = engine.handle_text(CHAT, "где вы находитесь?")

    assert "Навои" in reply.text


def test_first_contact_faq_still_goes_through_llm(
        app_session_factory, admin_engine, clinic_a, doctor_a):
    # язык ещё не выбран — детект языка нужен, путь через NLU сохраняется
    set_address(admin_engine, clinic_a, "Ташкент, ул. Навои, 10")
    engine, _ = make(app_session_factory, clinic_a,
                     [extr(intent="question", language="uz")])
    reply = engine.handle_text(CHAT, "manzilingiz qayerda?")

    assert "Навои" in reply.text
    assert "Manzilimiz" in reply.text, "язык взят из NLU-детекта (uz)"


# ── Адрес ────────────────────────────────────────────────────────────────────

def set_address(admin_engine, clinic_id, value):
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE clinic SET address = :a WHERE id = :c"),
                     {"a": value, "c": clinic_id})


def test_address_question_answers_when_set(app_session_factory, admin_engine,
                                           clinic_a, doctor_a):
    set_address(admin_engine, clinic_a, "Ташкент, ул. Навои, 10")
    engine, notifier = make(app_session_factory, clinic_a,
                            [extr(intent="question")])
    reply = engine.handle_text(CHAT, "где вы находитесь?")

    assert "Навои" in reply.text
    assert notifier.calls == []
    assert saved_questions(admin_engine) == []


def test_address_question_without_address_falls_to_not_understood(
        app_session_factory, admin_engine, clinic_a, doctor_a):
    engine, notifier = make(app_session_factory, clinic_a,
                            [extr(intent="question")])
    reply = engine.handle_text(CHAT, "какой у вас адрес?")

    assert [b.action for b in reply.buttons] == ["call_admin"], \
        "адрес не задан — честное «не понял» с кнопкой к человеку"
    assert notifier.calls == []
    assert len(saved_questions(admin_engine)) == 1, \
        "вопрос без ответа копится для владельца"


# ── Копилка неотвеченных вопросов ────────────────────────────────────────────

def test_unanswered_question_saved_with_phone_masked(
        app_session_factory, admin_engine, clinic_a, doctor_a):
    engine, _ = make(app_session_factory, clinic_a, [extr(intent="question")])
    engine.handle_text(CHAT, "можно оплатить переводом на +998901234567?")

    saved = saved_questions(admin_engine)
    assert len(saved) == 1
    assert "998901234567" not in saved[0] and "[phone]" in saved[0]


# ── Дайджест ─────────────────────────────────────────────────────────────────

def test_digest_includes_unanswered_questions(app_session_factory, admin_engine,
                                              clinic_a, doctor_a):
    engine, _ = make(app_session_factory, clinic_a,
                     [extr(intent="question"), extr(intent="question")])
    engine.handle_text(CHAT, "вы принимаете карты?")
    engine.handle_text(CHAT, "есть ли парковка?")

    api = FakeTelegramAPI()
    service = ReminderService(app_session_factory, clinic_a, tg_api=api,
                              digest_chat_id=ADMIN_CHAT)
    evening = datetime.now(TASHKENT).replace(hour=21, minute=30)
    assert service.maybe_send_digest(now_local=evening) is True

    digest = api.sent[-1][1]
    assert "Вопросы без ответа" in digest
    assert "карты" in digest and "парковка" in digest


def test_digest_without_questions_has_no_block(app_session_factory, admin_engine,
                                               clinic_a, doctor_a):
    api = FakeTelegramAPI()
    service = ReminderService(app_session_factory, clinic_a, tg_api=api,
                              digest_chat_id=ADMIN_CHAT)
    evening = datetime.now(TASHKENT).replace(hour=21, minute=30)
    assert service.maybe_send_digest(now_local=evening) is True
    assert "Вопросы без ответа" not in api.sent[-1][1]


# ── Retention и онбординг ────────────────────────────────────────────────────

def test_retention_cleans_old_questions(app_session_factory, admin_engine,
                                        clinic_a):
    from navbat.retention import cleanup_old_data
    with admin_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO unanswered_question (clinic_id, question, at) VALUES "
            "(:c, 'старый вопрос', now() - interval '100 days'), "
            "(:c, 'свежий вопрос', now())"), {"c": clinic_a})

    cleanup_old_data(app_session_factory, clinic_a, days=90)
    assert saved_questions(admin_engine) == ["свежий вопрос"]


def test_onboard_sets_address(app_session_factory, admin_engine, clinic_a):
    from navbat.onboard import set_clinic_address
    set_clinic_address(app_session_factory, clinic_a, "Ташкент, Чиланзар 5")
    with admin_engine.begin() as conn:
        address = conn.execute(text("SELECT address FROM clinic WHERE id = :c"),
                               {"c": clinic_a}).scalar_one()
    assert address == "Ташкент, Чиланзар 5"
