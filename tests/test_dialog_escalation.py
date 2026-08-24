"""Стоп-состояние escalated и выход из него.

С П-2а эскалацию вызывает прямая просьба человека (или двойной сбой
confirm — tests/test_soft_escalations.py); кривые ответы NLU дают
переспрос/«не понял» с кнопками и админа не дёргают. Здесь — жизненный
цикл заморозки: вход по просьбе, стоп-состояние, выход через /start.
"""
from __future__ import annotations

from conftest import next_monday
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.replies import TEMPLATES
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


def test_escalated_state_stops_processing(app_session_factory, admin_engine, clinic_a,
                                          doctor_a, service_cleaning):
    # пустой script: любой вызов экстрактора упал бы ошибкой —
    # в escalated NLU вообще не должен дёргаться
    engine, notifier = _engine(app_session_factory, clinic_a, [])
    engine.handle_text(CHAT, "позовите администратора")
    assert fsm_state(admin_engine) == "escalated"

    reply = engine.handle_text(CHAT, "запишите на чистку")
    assert fsm_state(admin_engine) == "escalated"
    assert len(notifier.calls) == 1, "повторных эскалаций нет"
    # сценарий не продолжается: ни слотов, ни выбора услуги — единственная
    # кнопка ведёт обратно к боту (карта продажи №5, было «кнопок нет вовсе»)
    assert [b.action for b in reply.buttons] == ["unfreeze"]
    assert not reply.button_rows


def test_valid_extraction_resets_failure_counter(app_session_factory, admin_engine,
                                                 clinic_a, doctor_a, service_cleaning):
    engine, notifier = _engine(app_session_factory, clinic_a, [
        ExtractionError("raz"),
        extr(service="cleaning", date_ref=explicit(next_monday())),
        ExtractionError("dva"),
    ])
    engine.handle_text(CHAT, "абракадабра")
    engine.handle_text(CHAT, "чистку в понедельник")  # валидный — счётчик в ноль
    reply = engine.handle_text(CHAT, "абракадабра")

    assert not notifier.calls
    assert fsm_state(admin_engine) != "escalated"
    # пересмотр 11.06: посреди сценария 1-й сбой = reask + повтор текущего
    # шага (не голый reask); главное — счётчик сброшен и это НЕ «не понял»
    assert TEMPLATES["reask"]["ru"] in reply.text, \
        "после сброса счётчика одиночный сбой — мягкий переспрос (reask)"
    assert TEMPLATES["not_understood"]["ru"] not in reply.text


# ── Выход из escalated: /start пациентом (Ф1.5, BRIEF разд. 14.A) ────────────

def test_start_releases_escalated_and_resets_counter(app_session_factory,
                                                     admin_engine, clinic_a,
                                                     doctor_a, service_cleaning):
    engine, notifier = _engine(app_session_factory, clinic_a,
                               [ExtractionError("raz")])
    engine.handle_action(CHAT, "lang:ru")  # язык выбран кнопкой
    engine.handle_text(CHAT, "позовите администратора")
    assert fsm_state(admin_engine) == "escalated"

    released = engine.handle_text(CHAT, "/start")
    assert fsm_state(admin_engine) == "idle"
    assert released.menu, "после разморозки — приветствие с главным меню"

    # счётчик сброшен: одиночный сбой NLU — переспрос, бот работает дальше
    engine.handle_text(CHAT, "снова абракадабра")
    assert fsm_state(admin_engine) != "escalated"
    assert len(notifier.calls) == 1, "повторной эскалации нет"


def test_start_in_escalated_without_lang_shows_lang_screen(app_session_factory,
                                                           admin_engine, clinic_a,
                                                           doctor_a, service_cleaning):
    engine, _ = _engine(app_session_factory, clinic_a, [])
    engine.handle_text(CHAT, "позовите администратора")
    assert fsm_state(admin_engine) == "escalated"

    reply = engine.handle_text(CHAT, "/start")
    assert fsm_state(admin_engine) == "idle"
    assert [b.action for b in reply.buttons] == ["lang:uz", "lang:ru"]


def test_repeat_escalation_within_cooldown_is_silent(app_session_factory,
                                                     admin_engine, clinic_a,
                                                     doctor_a, service_cleaning):
    """Карусель «позовите → /start → позовите» жужжала админу на каждом круге.

    Первый алерт уходит мгновенно (P0 «до человека один тап»), повторный в
    кулдауне — тихо: пациент видит ту же заморозку, дубль алерта не шлётся."""
    engine, notifier = _engine(app_session_factory, clinic_a, [])
    engine.handle_text(CHAT, "позовите администратора")
    engine.handle_text(CHAT, "/start")  # пациент сам разморозился
    reply = engine.handle_text(CHAT, "позовите администратора")

    assert fsm_state(admin_engine) == "escalated", "заморозка работает как раньше"
    assert len(notifier.calls) == 1, "дубль алерта в кулдауне не уходит"
    assert reply.text, "пациенту — обычный ответ эскалации"


def test_repeat_escalation_after_cooldown_alerts_again(app_session_factory,
                                                       admin_engine, clinic_a,
                                                       doctor_a, service_cleaning):
    from datetime import datetime, timedelta, timezone

    from navbat.dialog.dialog_common import ESCALATION_ALERT_COOLDOWN
    from test_dialog_booking import RecordingNotifier

    now = {"t": datetime.now(timezone.utc)}
    notifier = RecordingNotifier()
    engine = DialogEngine(app_session_factory, clinic_a,
                          extractor=FakeExtractor(script=[]),
                          notifier=notifier, clock=lambda: now["t"])
    engine.handle_text(CHAT, "позовите администратора")
    engine.handle_text(CHAT, "/start")
    now["t"] += ESCALATION_ALERT_COOLDOWN + timedelta(minutes=1)
    engine.handle_text(CHAT, "позовите администратора")

    assert len(notifier.calls) == 2, "просьба после кулдауна — снова алерт"


def test_failed_alert_delivery_does_not_start_cooldown(app_session_factory,
                                                       admin_engine, clinic_a,
                                                       doctor_a,
                                                       service_cleaning):
    """Ревью (major): notify глотает сбой доставки — если алерт НИКУДА не
    дошёл, кулдаун стартовать не должен, иначе транзиентная авария канала
    глушит «до человека один тап» на два часа."""
    class UndeliveredNotifier(RecordingNotifier):
        def notify(self, chat_id, reason, context):
            super().notify(chat_id, reason, context)
            return False  # все админ-чаты недоступны

    notifier = UndeliveredNotifier()
    engine = DialogEngine(app_session_factory, clinic_a,
                          extractor=FakeExtractor(script=[]),
                          notifier=notifier)
    engine.handle_text(CHAT, "позовите администратора")
    engine.handle_text(CHAT, "/start")
    engine.handle_text(CHAT, "позовите администратора")

    assert len(notifier.calls) == 2, "недоставленный алерт не включает кулдаун"
