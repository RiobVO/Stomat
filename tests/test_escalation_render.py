"""Эскалация админу показывает читаемый контекст брони, а не сырой JSON (M3)."""
from __future__ import annotations

from navbat.telegram.escalation import TelegramEscalation, summarize_context


class _Api:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


def test_summary_renders_booking_fields_human_readable():
    summary = summarize_context(
        {"service": "cleaning", "date": "2026-06-10", "time_ref": "14:00",
         "lang": "ru", "nlu_failures": 1})  # внутренние поля — не показываем
    assert "Чистка" in summary          # услуга по-русски, не «cleaning»
    assert "10.06" in summary           # дата dd.mm, не ISO
    assert "14:00" in summary
    assert "lang" not in summary and "nlu" not in summary


def test_summary_empty_when_nothing_chosen():
    assert summarize_context({"lang": "uz"}) == "пациент ещё ничего не выбрал"


def test_notify_message_actionable_and_no_raw_json():
    api = _Api()
    TelegramEscalation(api, admin_chat_id=999).notify(
        12345, "2 кривых ответа NLU подряд",
        {"service": "implant", "date": "2026-06-11", "lang": "ru"})
    chat_id, msg = api.sent[0]
    assert chat_id == 999
    assert "чат 12345" in msg
    assert "/release 12345" in msg
    assert "Имплант" in msg
    assert "{" not in msg and "}" not in msg  # без сырого JSON-дампа


def test_notify_without_admin_chat_does_not_send():
    api = _Api()
    TelegramEscalation(api, admin_chat_id=None).notify(1, "x", {})
    assert api.sent == []


def test_notify_fans_out_to_all_admins():
    # M4: алерт уходит КАЖДОМУ админ-чату, не одному
    api = _Api()
    TelegramEscalation(api, [111, 222, 333]).notify(5, "повод", {})
    assert [chat for chat, _ in api.sent] == [111, 222, 333]
