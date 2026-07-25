"""Эскалация в Telegram-чат админа (P0 BRIEF): контекст уходит человеку."""
from __future__ import annotations

from navbat.telegram.escalation import TelegramEscalation
from test_tg_worker import FakeTelegramAPI

ADMIN_CHAT = 777


def test_notify_sends_context_to_admin_chat():
    api = FakeTelegramAPI()
    escalation = TelegramEscalation(api, admin_chat_id=ADMIN_CHAT)
    escalation.notify(100, "2 кривых ответа NLU подряд", {"lang": "uz"})

    assert len(api.sent) == 1
    chat_id, message_text, _ = api.sent[0]
    assert chat_id == ADMIN_CHAT
    assert "100" in message_text
    assert "2 кривых ответа NLU подряд" in message_text
    assert "uz" in message_text


def test_alert_contains_release_hint():
    # админу не нужно помнить синтаксис — команда снятия прямо в алерте
    api = FakeTelegramAPI()
    escalation = TelegramEscalation(api, admin_chat_id=ADMIN_CHAT)
    escalation.notify(100, "2 кривых ответа NLU подряд", {"lang": "uz"})

    assert "/release 100" in api.sent[0][1]


def test_no_admin_chat_falls_back_to_log_without_raising():
    api = FakeTelegramAPI()
    escalation = TelegramEscalation(api, admin_chat_id=None)
    escalation.notify(100, "причина", {})
    assert api.sent == []


def test_api_failure_does_not_break_processing():
    api = FakeTelegramAPI()
    api.send_failures = 1
    escalation = TelegramEscalation(api, admin_chat_id=ADMIN_CHAT)
    escalation.notify(100, "причина", {})  # не должно бросить
    assert api.sent == []
