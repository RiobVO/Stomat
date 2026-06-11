"""Визуал v2 (П-7, стиль маникюр-бота): HTML-разметка, hero-карточки,
эмодзи-якоря, экранирование подстановок.

parse_mode=HTML уходит ТОЛЬКО пациентскими путями и дайджестом —
алерты эскалаций остаются plain (сырой контекст не должен ломать парсер).
"""
from __future__ import annotations

import json

from conftest import at_tashkent, next_monday
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.replies import SERVICE_EMOJI, Reply, t
from navbat.nlu.extractor import FakeExtractor
from navbat.stats import render_questions
from navbat.telegram.worker import send_reply
from test_dialog_booking import (
    CHAT, RecordingNotifier, explicit, extr, slot_buttons)
from test_tg_api import make_api, ok_response
from test_tg_worker import FakeTelegramAPI


# ── parse_mode ───────────────────────────────────────────────────────────────

def test_send_message_passes_parse_mode():
    api, requests = make_api(lambda req, n: ok_response({"message_id": 1}))
    api.send_message(100, "<b>Жирный</b>", parse_mode="HTML")
    assert json.loads(requests[0].content)["parse_mode"] == "HTML"


def test_send_message_default_is_plain():
    api, requests = make_api(lambda req, n: ok_response({"message_id": 1}))
    api.send_message(100, "контекст с <сырыми> скобками")
    assert "parse_mode" not in json.loads(requests[0].content)


def test_send_reply_uses_html(app_session_factory, clinic_a):
    class CapturingAPI(FakeTelegramAPI):
        def __init__(self):
            super().__init__()
            self.parse_modes = []

        def send_message(self, *args, parse_mode=None, **kwargs):
            self.parse_modes.append(parse_mode)
            return super().send_message(*args, **kwargs)

    api = CapturingAPI()
    send_reply(api, app_session_factory, clinic_a, CHAT, Reply("привет"))
    assert api.parse_modes == ["HTML"], "пациентский путь — всегда HTML"


# ── Экранирование ────────────────────────────────────────────────────────────

def test_t_escapes_substitutions():
    out = t("clinic_address", "ru", address="<script>alert(1)</script> & Co")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out and "&amp; Co" in out
    assert "📍" in out, "разметка шаблона не пострадала"


def test_render_questions_escapes_patient_text():
    out = render_questions(["а можно <b>жирно</b>?"])
    assert "<b>жирно</b>" not in out
    assert out.startswith("❓ <b>Вопросы без ответа"), \
        "наша разметка остаётся, пациентская — экранируется"


# ── Карточки и кнопки ────────────────────────────────────────────────────────

def test_booked_card_with_doctor_line(app_session_factory, admin_engine,
                                      clinic_a, service_cleaning):
    from conftest import make_doctor
    from navbat.dialog.patients import create_patient
    from navbat.db.base import tenant_transaction

    make_doctor(admin_engine, clinic_a, name="Алиев")  # именованный врач
    with tenant_transaction(app_session_factory, clinic_a) as session:
        create_patient(session, CHAT, "Гульнора", "998901112233")
    monday = next_monday()
    engine = DialogEngine(
        app_session_factory, clinic_a,
        extractor=FakeExtractor(script=[extr(service="cleaning",
                                             date_ref=explicit(monday))]),
        notifier=RecordingNotifier(),
        clock=lambda: at_tashkent(monday, "08:00"))
    offer = engine.handle_text(CHAT, "чистка в понедельник")
    done = engine.handle_action(CHAT, slot_buttons(offer)[0].action)

    assert "<b>ЗАПИСЬ ПОДТВЕРЖДЕНА</b>" in done.text
    assert "🦷 Чистка" in done.text
    assert "\n👨‍⚕️ Алиев" in done.text, "врач — отдельной строкой карточки"
    assert "🔔" in done.text


def test_service_buttons_have_emoji(app_session_factory, admin_engine,
                                    clinic_a, doctor_a, service_cleaning):
    engine = DialogEngine(app_session_factory, clinic_a,
                          extractor=FakeExtractor(script=[]),
                          notifier=RecordingNotifier())
    engine.handle_action(CHAT, "lang:ru")
    reply = engine.handle_text(CHAT, t("btn_menu_book", "ru"))

    labels = [b.label for b in reply.buttons]
    assert labels and all(any(e in lab for e in SERVICE_EMOJI.values())
                          for lab in labels), \
        f"кнопки услуг с эмодзи-префиксом: {labels}"


def test_greeting_hero(app_session_factory, admin_engine, clinic_a):
    engine = DialogEngine(app_session_factory, clinic_a,
                          extractor=FakeExtractor(script=[]),
                          notifier=RecordingNotifier())
    reply = engine.handle_action(CHAT, "lang:ru")
    assert reply.text.startswith("🦷 <b>")
    assert "👇" in reply.text
