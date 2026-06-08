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


def test_reask_keeps_menu_available(app_session_factory, admin_engine, clinic_a,
                                    doctor_a, service_cleaning):
    # M7: не понятому пациенту всегда доступны кнопки самообслуживания —
    # «не понял» без выхода толкает к ранней эскалации-тупику
    engine, _ = _engine(app_session_factory, clinic_a, [ExtractionError("?")])
    engine.handle_action(CHAT, "lang:ru")  # greeting показан, не первый контакт
    reply = engine.handle_text(CHAT, "абракадабра")
    assert reply.menu, "reask должен предлагать меню"


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


# ── Выход из escalated: /start пациентом (Ф1.5, BRIEF разд. 14.A) ────────────

def test_start_releases_escalated_and_resets_counter(app_session_factory,
                                                     admin_engine, clinic_a,
                                                     doctor_a, service_cleaning):
    engine, notifier = _engine(app_session_factory, clinic_a, [
        ExtractionError("raz"), ExtractionError("dva"), ExtractionError("tri"),
    ])
    engine.handle_action(CHAT, "lang:ru")  # язык выбран кнопкой
    engine.handle_text(CHAT, "абракадабра")
    engine.handle_text(CHAT, "опять абракадабра")
    assert fsm_state(admin_engine) == "escalated"

    released = engine.handle_text(CHAT, "/start")
    assert fsm_state(admin_engine) == "idle"
    assert released.menu, "после разморозки — приветствие с главным меню"

    # счётчик сброшен: одиночный сбой NLU — переспрос, не мгновенная эскалация
    engine.handle_text(CHAT, "снова абракадабра")
    assert fsm_state(admin_engine) != "escalated"
    assert len(notifier.calls) == 1, "повторной эскалации нет"


def test_start_in_escalated_without_lang_shows_lang_screen(app_session_factory,
                                                           admin_engine, clinic_a,
                                                           doctor_a, service_cleaning):
    engine, _ = _engine(app_session_factory, clinic_a,
                        [ExtractionError("raz"), ExtractionError("dva")])
    engine.handle_text(CHAT, "абракадабра")
    engine.handle_text(CHAT, "опять абракадабра")
    assert fsm_state(admin_engine) == "escalated"

    reply = engine.handle_text(CHAT, "/start")
    assert fsm_state(admin_engine) == "idle"
    assert [b.action for b in reply.buttons] == ["lang:uz", "lang:ru"]
