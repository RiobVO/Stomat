"""Эскалация в Telegram-чат админа (P0 BRIEF): контекст уходит человеку."""
from __future__ import annotations

from navbat.telegram.escalation import TelegramEscalation
from test_tg_worker import FakeTelegramAPI

ADMIN_CHAT = 777


def test_notify_sends_context_to_admin_chat():
    api = FakeTelegramAPI()
    escalation = TelegramEscalation(api, admin_chat_id=ADMIN_CHAT)
    escalation.notify(100, "2 кривых ответа NLU подряд",
                      {"service": "cleaning", "lang": "uz"})

    assert len(api.sent) == 1
    chat_id, message_text, _ = api.sent[0]
    assert chat_id == ADMIN_CHAT
    assert "100" in message_text
    assert "2 кривых ответа NLU подряд" in message_text
    # M3: бронь-контекст читаемо (услуга по-русски), а не сырой JSON c 'uz'
    assert "Чистка" in message_text


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


# ── C-3: системные алерты — админ-чаты + канал владельца ────────────────────

def test_notify_system_fans_to_admins_and_owner(monkeypatch):
    monkeypatch.setenv("NAVBAT_OWNER_CHAT_ID", "555")
    api = FakeTelegramAPI()
    esc = TelegramEscalation(api, [111, 222])
    esc.notify_system("синк умер", {})
    chats = [entry[0] for entry in api.sent]
    assert chats == [111, 222, 555]
    assert "Системный алерт" in api.sent[0][1]


def test_notify_system_without_owner_env(monkeypatch):
    monkeypatch.delenv("NAVBAT_OWNER_CHAT_ID", raising=False)
    api = FakeTelegramAPI()
    esc = TelegramEscalation(api, [111])
    esc.notify_system("cap исчерпан", {})
    assert [entry[0] for entry in api.sent] == [111]


def test_system_alert_falls_back_to_notify():
    from navbat.dialog.escalation import system_alert
    from test_dialog_booking import RecordingNotifier

    notifier = RecordingNotifier()
    system_alert(notifier, "проблема", {}, chat_id=42)
    assert notifier.calls == [(42, "проблема")]
