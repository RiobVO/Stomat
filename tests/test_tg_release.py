"""Команда админа /release <chat_id>: снятие эскалации из админ-чата (Ф1.5).

Снятие переводит conversation в idle и возвращает пациенту главное меню —
пациент должен видеть, что бот снова отвечает.
"""
from __future__ import annotations

from navbat.dialog.replies import TEMPLATES
from navbat.nlu.extractor import ExtractionError
from test_dialog_booking import CHAT, explicit, extr, fsm_state
from test_tg_worker import make_worker, put_message
from conftest import next_monday

ADMIN_CHAT = 777


def escalate_chat(worker, app_session_factory, clinic_id):
    """Два сбоя NLU подряд → чат пациента в escalated."""
    put_message(app_session_factory, clinic_id, "абракадабра")
    put_message(app_session_factory, clinic_id, "опять абракадабра")
    worker.process_one()
    worker.process_one()


def sent_to(api, chat_id):
    """Сообщения (text, menu) ушедшие в конкретный чат."""
    return [(text, keyboard[1])
            for (chat, text, _), keyboard in zip(api.sent, api.keyboards)
            if chat == chat_id]


def test_release_from_admin_unfreezes_chat(app_session_factory, admin_engine,
                                           clinic_a, doctor_a, service_cleaning):
    worker, api, _ = make_worker(
        app_session_factory, clinic_a,
        [ExtractionError("raz"), ExtractionError("dva")],
        admin_chat_id=ADMIN_CHAT)
    escalate_chat(worker, app_session_factory, clinic_a)
    assert fsm_state(admin_engine) == "escalated"

    put_message(app_session_factory, clinic_a, f"/release {CHAT}",
                chat_id=ADMIN_CHAT)
    worker.process_one()

    assert fsm_state(admin_engine) == "idle"
    admin_msgs = sent_to(api, ADMIN_CHAT)
    assert admin_msgs and "[OK]" in admin_msgs[-1][0]
    patient_text, patient_menu = sent_to(api, CHAT)[-1]
    assert patient_text == TEMPLATES["menu_hint"]["ru"]
    assert patient_menu, "пациенту вернулось главное меню"


def test_release_resets_failure_counter(app_session_factory, admin_engine,
                                        clinic_a, doctor_a, service_cleaning):
    # после снятия один сбой NLU — переспрос, не мгновенная повторная эскалация
    worker, api, notifier = make_worker(
        app_session_factory, clinic_a,
        [ExtractionError("raz"), ExtractionError("dva"), ExtractionError("tri")],
        admin_chat_id=ADMIN_CHAT)
    escalate_chat(worker, app_session_factory, clinic_a)
    put_message(app_session_factory, clinic_a, f"/release {CHAT}",
                chat_id=ADMIN_CHAT)
    worker.process_one()

    put_message(app_session_factory, clinic_a, "снова абракадабра")
    worker.process_one()
    assert fsm_state(admin_engine) != "escalated"
    assert len(notifier.calls) == 1, "повторной эскалации нет"


def test_release_unknown_chat(app_session_factory, admin_engine, clinic_a,
                              doctor_a, service_cleaning):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    put_message(app_session_factory, clinic_a, "/release 424242",
                chat_id=ADMIN_CHAT)
    worker.process_one()

    text, _ = sent_to(api, ADMIN_CHAT)[-1]
    assert "не найден" in text


def test_release_not_escalated_chat(app_session_factory, admin_engine, clinic_a,
                                    doctor_a, service_cleaning):
    worker, api, _ = make_worker(
        app_session_factory, clinic_a,
        [extr(service="cleaning", date_ref=explicit(next_monday()))],
        admin_chat_id=ADMIN_CHAT)
    put_message(app_session_factory, clinic_a, "чистку в понедельник")
    worker.process_one()  # обычный диалог, не escalated

    put_message(app_session_factory, clinic_a, f"/release {CHAT}",
                chat_id=ADMIN_CHAT)
    worker.process_one()

    text, _ = sent_to(api, ADMIN_CHAT)[-1]
    assert "не в эскалации" in text
    assert fsm_state(admin_engine) == "booking_offer_slots", "состояние не тронуто"


def test_release_bad_argument_shows_usage(app_session_factory, admin_engine,
                                          clinic_a, doctor_a, service_cleaning):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    put_message(app_session_factory, clinic_a, "/release", chat_id=ADMIN_CHAT)
    put_message(app_session_factory, clinic_a, "/release abc", chat_id=ADMIN_CHAT)
    worker.process_one()
    worker.process_one()

    for text, _ in sent_to(api, ADMIN_CHAT):
        assert "/release <chat_id>" in text


def test_release_from_patient_goes_to_nlu(app_session_factory, admin_engine,
                                          clinic_a, doctor_a, service_cleaning):
    # пациент написал «/release 100» — это обычный текст, не админ-команда
    worker, api, _ = make_worker(
        app_session_factory, clinic_a,
        [extr(service="cleaning", date_ref=explicit(next_monday()))],
        admin_chat_id=ADMIN_CHAT)
    put_message(app_session_factory, clinic_a, "/release 100", chat_id=CHAT)
    worker.process_one()

    assert api.sent[0][0] == CHAT
    assert any(b.action.startswith("a:") for b in api.sent[0][2]), \
        "текст ушёл в NLU и вернулись слоты"
