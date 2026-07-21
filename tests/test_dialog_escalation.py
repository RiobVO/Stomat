"""Эскалация: 2 кривых ответа NLU подряд — передача человеку, цикл обрывается."""
from __future__ import annotations

from conftest import next_monday
from navbat.dialog.fsm import DialogEngine
from navbat.nlu.extractor import ExtractionError, FakeExtractor
from test_dialog_booking import CHAT, RecordingNotifier, explicit, extr, fsm_state


def _engine(app_session_factory, clinic_id, script):
    notifier = RecordingNotifier()
    return DialogEngine(app_session_factory, clinic_id,
                        extractor=FakeExtractor(script=script),
                        notifier=notifier), notifier


def test_two_bad_json_in_a_row_escalates(app_session_factory, admin_engine, clinic_a,
                                         doctor_a, service_cleaning):
    engine, notifier = _engine(app_session_factory, clinic_a,
                               [ExtractionError("raz"), ExtractionError("dva")])

    first = engine.handle_text(CHAT, "абракадабра")
    assert not notifier.calls, "после первого сбоя — переспрос, не эскалация"
    assert fsm_state(admin_engine) != "escalated"

    second = engine.handle_text(CHAT, "снова абракадабра")
    assert len(notifier.calls) == 1
    assert fsm_state(admin_engine) == "escalated"
    assert first.text != second.text


def test_escalated_state_stops_processing(app_session_factory, admin_engine, clinic_a,
                                          doctor_a, service_cleaning):
    # пустой script: любой вызов экстрактора упал бы ошибкой —
    # в escalated NLU вообще не должен дёргаться
    engine, notifier = _engine(app_session_factory, clinic_a,
                               [ExtractionError("raz"), ExtractionError("dva")])
    engine.handle_text(CHAT, "абракадабра")
    engine.handle_text(CHAT, "абракадабра")

    reply = engine.handle_text(CHAT, "запишите на чистку")
    assert fsm_state(admin_engine) == "escalated"
    assert len(notifier.calls) == 1, "повторных эскалаций нет"
    assert not reply.buttons


def test_valid_extraction_resets_failure_counter(app_session_factory, admin_engine,
                                                 clinic_a, doctor_a, service_cleaning):
    engine, notifier = _engine(app_session_factory, clinic_a, [
        ExtractionError("raz"),
        extr(service="cleaning", date_ref=explicit(next_monday())),
        ExtractionError("dva"),
    ])
    engine.handle_text(CHAT, "абракадабра")
    engine.handle_text(CHAT, "чистку в понедельник")  # валидный — счётчик в ноль
    engine.handle_text(CHAT, "абракадабра")

    assert not notifier.calls
    assert fsm_state(admin_engine) != "escalated"
