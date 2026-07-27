"""Выход из заморозки одним тапом (карта docs/SALES_READINESS.md, №5).

Живая проверка 27.07.2026: после эскалации пациент жал «📅 Записаться»,
«💰 Цены» и писал текстом — на всё приходило одно «Передаю администратору».
Reply-меню оставалось на экране, но было мертво; единственный выход —
/start, о котором в тексте не сказано, либо /release от админа.

Право пациента выйти из заморозки самому уже зафиксировано (BRIEF разд.
14.A: «/start пациентом снимает заморозку») — кнопка лишь делает этот
выход видимым, стоп-состояние как правило не отменяет.
"""
from __future__ import annotations

from conftest import next_monday
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.replies import TEMPLATES
from navbat.nlu.extractor import ExtractionError, FakeExtractor
from test_dialog_booking import CHAT, RecordingNotifier, explicit, extr, fsm_state

EXIT = "unfreeze"


def _engine(app_session_factory, clinic_id, script):
    notifier = RecordingNotifier()
    return DialogEngine(app_session_factory, clinic_id,
                        extractor=FakeExtractor(script=script),
                        notifier=notifier), notifier


def _actions(reply) -> list[str]:
    flat = [b.action for b in reply.buttons]
    return flat + [b.action for row in reply.button_rows for b in row]


def test_escalation_answer_offers_way_back(app_session_factory, admin_engine,
                                           clinic_a, doctor_a, service_cleaning):
    engine, _ = _engine(app_session_factory, clinic_a, [])
    reply = engine.handle_text(CHAT, "позовите администратора")

    assert fsm_state(admin_engine) == "escalated"
    assert EXIT in _actions(reply), "у пациента должен быть выход одним тапом"


def test_frozen_dialog_keeps_offering_way_back(app_session_factory, admin_engine,
                                               clinic_a, doctor_a,
                                               service_cleaning):
    """Пациент мог пропустить первое сообщение — кнопка обязана приходить
    и дальше, иначе он остаётся запертым ровно как раньше."""
    engine, notifier = _engine(app_session_factory, clinic_a, [])
    engine.handle_text(CHAT, "позовите администратора")

    again = engine.handle_text(CHAT, "ну и сколько ждать?")

    assert fsm_state(admin_engine) == "escalated", "заморозка не снимается сама"
    assert len(notifier.calls) == 1, "повторных алертов админу нет"
    assert EXIT in _actions(again)


def test_exit_button_unfreezes_and_shows_menu(app_session_factory, admin_engine,
                                              clinic_a, doctor_a,
                                              service_cleaning):
    engine, _ = _engine(app_session_factory, clinic_a, [])
    engine.handle_action(CHAT, "lang:ru")
    engine.handle_text(CHAT, "позовите администратора")

    released = engine.handle_action(CHAT, EXIT)

    assert fsm_state(admin_engine) == "idle"
    assert released.menu, "после возврата — главное меню, как после /start"


def test_patient_can_book_after_coming_back(app_session_factory, admin_engine,
                                            clinic_a, doctor_a, service_cleaning):
    """Возврат обязан быть полноценным: пациент дальше записывается."""
    engine, notifier = _engine(app_session_factory, clinic_a, [
        extr(service="cleaning", date_ref=explicit(next_monday())),
    ])
    engine.handle_action(CHAT, "lang:ru")
    engine.handle_text(CHAT, "позовите администратора")
    engine.handle_action(CHAT, EXIT)

    reply = engine.handle_text(CHAT, "чистку в понедельник")

    assert fsm_state(admin_engine) != "escalated"
    assert any(a.startswith("slot:") for a in _actions(reply)), \
        "после возврата сценарий записи работает целиком"
    assert len(notifier.calls) == 1


def test_counter_reset_on_exit(app_session_factory, admin_engine, clinic_a,
                               doctor_a, service_cleaning):
    """Счётчик сбоев обнуляется, как на /start: иначе первый же непонятый
    текст после возврата снова уводил бы в «не понял» с кнопкой к человеку."""
    engine, _ = _engine(app_session_factory, clinic_a, [ExtractionError("raz")])
    engine.handle_action(CHAT, "lang:ru")
    engine.handle_text(CHAT, "позовите администратора")
    engine.handle_action(CHAT, EXIT)

    reply = engine.handle_text(CHAT, "абракадабра")

    assert TEMPLATES["reask"]["ru"] in reply.text, \
        "после возврата счёт с нуля — мягкий переспрос, не «не понял»"


def test_exit_button_outside_escalation_is_harmless(app_session_factory,
                                                    admin_engine, clinic_a,
                                                    doctor_a, service_cleaning):
    """Кнопка из старого сообщения не должна ломать живой диалог."""
    engine, _ = _engine(app_session_factory, clinic_a, [])
    engine.handle_action(CHAT, "lang:ru")

    reply = engine.handle_action(CHAT, EXIT)

    assert fsm_state(admin_engine) == "idle"
    assert reply.menu
