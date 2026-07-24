"""Телефон пациента — только кнопкой «Поделиться контактом» (request_contact).

Ручной ввод номера не принимается (решение: без fallback) — текст на шаге
телефона вежливо возвращает кнопку. Чужой контакт (own=False) отклоняется.
Бонус PII: телефон текстом в NLU не попадает.
"""
from __future__ import annotations

from sqlalchemy import text

from conftest import make_service, next_monday
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.replies import TEMPLATES
from navbat.nlu.extractor import FakeExtractor
from test_dialog_booking import (
    CHAT,
    RecordingNotifier,
    appt_status,
    explicit,
    extr,
    fsm_state,
    make_engine,
    slot_buttons,
)


class CountingExtractor:
    """FakeExtractor + журнал вызовов: проверяем, что NLU не дёргается."""

    def __init__(self, script) -> None:
        self._inner = FakeExtractor(script=script)
        self.calls: list[str] = []

    def extract(self, message: str):
        self.calls.append(message)
        return self._inner.extract(message)


def booking_script(extra=()):
    return [extr(service="cleaning", date_ref=explicit(next_monday())), *extra]


def to_phone_step(engine: DialogEngine) -> None:
    """Доводит нового пациента до шага awaiting_phone."""
    offer = engine.handle_text(CHAT, "хочу чистку в понедельник")
    engine.handle_action(CHAT, slot_buttons(offer)[0].action)
    engine.handle_text(CHAT, "Алишер")


def patient_count(admin_engine) -> int:
    with admin_engine.begin() as conn:
        return conn.execute(text("SELECT count(*) FROM patient")).scalar_one()


# ── Кнопка предлагается, контакт принимается ─────────────────────────────────

def test_ask_phone_offers_contact_button(app_session_factory, admin_engine,
                                         clinic_a, doctor_a, service_cleaning):
    engine = make_engine(app_session_factory, clinic_a, booking_script())
    offer = engine.handle_text(CHAT, "хочу чистку в понедельник")
    engine.handle_action(CHAT, slot_buttons(offer)[0].action)

    ask_phone = engine.handle_text(CHAT, "Алишер")
    assert ask_phone.contact_request == TEMPLATES["btn_share_contact"]["ru"]
    assert not ask_phone.buttons, "reply-клавиатура и inline несовместимы"
    assert fsm_state(admin_engine) == "awaiting_phone"


def test_own_contact_books_and_removes_keyboard(app_session_factory, admin_engine,
                                                clinic_a, doctor_a, service_cleaning):
    engine = make_engine(app_session_factory, clinic_a, booking_script())
    to_phone_step(engine)

    # Telegram отдаёт phone_number без плюса
    done = engine.handle_contact(CHAT, "998901234567", own=True)
    assert done.remove_keyboard, "клавиатура с кнопкой убирается"
    assert "09:00" in done.text
    assert appt_status(admin_engine) == "booked"
    assert fsm_state(admin_engine) == "idle"
    with admin_engine.begin() as conn:
        linked = conn.execute(text(
            "SELECT p.id FROM appointment a JOIN patient p ON p.id = a.patient_id"
        )).one_or_none()
    assert linked is not None, "пациент создан и привязан к записи"


# ── Отказы: чужой контакт, ручной текст ──────────────────────────────────────

def test_foreign_contact_rejected(app_session_factory, admin_engine,
                                  clinic_a, doctor_a, service_cleaning):
    engine = make_engine(app_session_factory, clinic_a, booking_script())
    to_phone_step(engine)

    reply = engine.handle_contact(CHAT, "998909999999", own=False)
    assert TEMPLATES["foreign_contact"]["ru"] in reply.text
    assert reply.contact_request, "кнопка предлагается снова"
    assert fsm_state(admin_engine) == "awaiting_phone"
    assert patient_count(admin_engine) == 0


def test_manual_valid_phone_rejected(app_session_factory, admin_engine,
                                     clinic_a, doctor_a, service_cleaning):
    engine = make_engine(app_session_factory, clinic_a, booking_script())
    to_phone_step(engine)

    reply = engine.handle_text(CHAT, "+998 90 123-45-67")
    assert TEMPLATES["press_contact_button"]["ru"] in reply.text
    assert reply.contact_request
    assert fsm_state(admin_engine) == "awaiting_phone"
    assert patient_count(admin_engine) == 0


def test_manual_text_does_not_hit_nlu(app_session_factory, admin_engine,
                                      clinic_a, doctor_a, service_cleaning):
    extractor = CountingExtractor(booking_script())
    engine = DialogEngine(app_session_factory, clinic_a, extractor=extractor,
                          notifier=RecordingNotifier())
    to_phone_step(engine)

    engine.handle_text(CHAT, "+998 90 123-45-67")
    assert len(extractor.calls) == 1, "NLU дёргался только на первом сообщении"
    assert patient_count(admin_engine) == 0, "текст не принят как телефон"


def test_question_on_phone_step_answers_and_reprompts_button(
        app_session_factory, admin_engine, clinic_a, doctor_a):
    make_service(admin_engine, clinic_a, "cleaning", 30, price=350_000)
    engine = make_engine(app_session_factory, clinic_a, booking_script(
        extra=[extr(intent="question", service="cleaning")]))
    to_phone_step(engine)

    reply = engine.handle_text(CHAT, "сколько стоит чистка?")
    assert "350 000" in reply.text
    assert TEMPLATES["ask_phone"]["ru"] in reply.text
    assert reply.contact_request, "повтор шага — снова с кнопкой"
    assert fsm_state(admin_engine) == "awaiting_phone"


# ── Края: не-узбекский номер, контакт вне шага ───────────────────────────────

def test_own_contact_foreign_number_escalates(app_session_factory, admin_engine,
                                              clinic_a, doctor_a, service_cleaning):
    notifier = RecordingNotifier()
    engine = DialogEngine(app_session_factory, clinic_a,
                          extractor=FakeExtractor(script=booking_script()),
                          notifier=notifier)
    to_phone_step(engine)

    reply = engine.handle_contact(CHAT, "+79161234567", own=True)
    assert TEMPLATES["escalated"]["ru"] in reply.text
    assert fsm_state(admin_engine) == "escalated"
    assert notifier.calls, "лид с иностранным номером уходит администратору"
    assert patient_count(admin_engine) == 0


def test_contact_in_idle_is_fallback(app_session_factory, admin_engine,
                                     clinic_a, doctor_a, service_cleaning):
    engine = make_engine(app_session_factory, clinic_a, [])
    reply = engine.handle_contact(CHAT, "998901234567", own=True)
    assert TEMPLATES["other_fallback"]["ru"] in reply.text
    assert patient_count(admin_engine) == 0
