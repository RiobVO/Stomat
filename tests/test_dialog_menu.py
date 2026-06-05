"""Кнопочный вход: /start, выбор языка, главное меню — всё до NLU.

Нажатие reply-кнопки приходит текстом; FSM матчит label (оба языка)
до экстрактора — CountingExtractor доказывает ноль вызовов NLU.
"""
from __future__ import annotations

from sqlalchemy import text

from conftest import next_monday
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.replies import menu_rows
from test_dialog_booking import (
    CHAT,
    RecordingNotifier,
    explicit,
    extr,
)
from test_dialog_contact import CountingExtractor


def counting_engine(app_session_factory, clinic_id, script=()):
    extractor = CountingExtractor(list(script))
    engine = DialogEngine(app_session_factory, clinic_id, extractor=extractor,
                          notifier=RecordingNotifier())
    return engine, extractor


# ── /start и язык ────────────────────────────────────────────────────────────

def test_start_first_time_offers_language_choice(app_session_factory, clinic_a):
    engine, extractor = counting_engine(app_session_factory, clinic_a)
    reply = engine.handle_text(CHAT, "/start")
    assert [b.action for b in reply.buttons] == ["lang:uz", "lang:ru"]
    assert "Tilni tanlang" in reply.text
    assert "Clinic A" not in reply.text, "приветствие — после выбора языка"
    assert extractor.calls == [], "/start не должен уходить в NLU"


def test_lang_choice_shows_greeting_with_menu(app_session_factory, admin_engine,
                                              clinic_a):
    engine, extractor = counting_engine(app_session_factory, clinic_a)
    engine.handle_text(CHAT, "/start")
    reply = engine.handle_action(CHAT, "lang:uz")
    assert reply.menu == menu_rows("uz")
    assert "Clinic A" in reply.text, "приветствие-дисклеймер (P0 BRIEF)"
    assert extractor.calls == []
    with admin_engine.begin() as conn:
        lang = conn.execute(text(
            "SELECT context ->> 'lang' FROM conversation WHERE tg_chat_id = :c"
        ), {"c": CHAT}).scalar_one()
    assert lang == "uz"


def test_start_repeat_skips_language_choice(app_session_factory, clinic_a):
    engine, _ = counting_engine(app_session_factory, clinic_a)
    engine.handle_text(CHAT, "/start")
    engine.handle_action(CHAT, "lang:ru")
    again = engine.handle_text(CHAT, "/start")
    assert again.menu == menu_rows("ru")
    assert not again.buttons, "язык уже выбран — сразу меню"


def test_start_after_text_dialog_keeps_detected_lang(app_session_factory, clinic_a,
                                                     doctor_a, service_cleaning):
    # пациент начал текстом (язык детектнут NLU) — /start не переспрашивает язык
    engine, _ = counting_engine(
        app_session_factory, clinic_a,
        script=[extr(service="cleaning", date_ref=explicit(next_monday()),
                     language="uz")])
    engine.handle_text(CHAT, "ertaga tish tozalashga yozilmoqchiman")
    reply = engine.handle_text(CHAT, "/start")
    assert reply.menu == menu_rows("uz")
    assert not reply.buttons
