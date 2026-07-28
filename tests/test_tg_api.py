"""Тонкий клиент Bot API: сборка запросов, retry на 429/5xx/сеть, ошибки API.

Сеть мокается httpx.MockTransport — реальных вызовов Telegram нет.
"""
from __future__ import annotations

import json

import httpx
import pytest

from navbat.dialog.replies import Button
from navbat.telegram.api import ChatUnavailableError, TelegramAPI, TelegramAPIError


def make_api(handler) -> tuple[TelegramAPI, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request, len(requests))

    client = httpx.Client(transport=httpx.MockTransport(recording_handler))
    return TelegramAPI("TOKEN", client=client, retry_delays=(0, 0, 0)), requests


def ok_response(result) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


# ── Сборка запросов ──────────────────────────────────────────────────────────

def test_send_message_builds_inline_keyboard():
    api, requests = make_api(lambda req, n: ok_response({"message_id": 1}))
    api.send_message(100, "Свободное время:",
                     buttons=[Button("09:00", "a:1"), Button("Другое время", "a:2")])

    request = requests[0]
    assert request.url.path.endswith("/botTOKEN/sendMessage")
    body = json.loads(request.content)
    assert body["chat_id"] == 100
    assert body["text"] == "Свободное время:"
    keyboard = body["reply_markup"]["inline_keyboard"]
    assert keyboard == [[{"text": "09:00", "callback_data": "a:1"}],
                       [{"text": "Другое время", "callback_data": "a:2"}]]


def test_send_message_builds_contact_keyboard():
    api, requests = make_api(lambda req, n: ok_response({"message_id": 1}))
    api.send_message(100, "Нажмите кнопку:", contact_request="📱 Отправить мой номер")

    markup = json.loads(requests[0].content)["reply_markup"]
    assert markup["keyboard"] == [[{"text": "📱 Отправить мой номер",
                                    "request_contact": True}]]
    assert markup["resize_keyboard"] is True
    assert markup["one_time_keyboard"] is True


def test_send_message_builds_persistent_menu_keyboard():
    api, requests = make_api(lambda req, n: ok_response({"message_id": 1}))
    api.send_message(100, "Меню:", menu=(("📅 Записаться",),
                                         ("💰 Цены", "🌐 Til / Язык")))
    markup = json.loads(requests[0].content)["reply_markup"]
    assert markup["keyboard"] == [[{"text": "📅 Записаться"}],
                                  [{"text": "💰 Цены"}, {"text": "🌐 Til / Язык"}]]
    assert markup["resize_keyboard"] is True
    assert markup["is_persistent"] is True


def test_send_message_without_buttons_has_no_markup():
    api, requests = make_api(lambda req, n: ok_response({"message_id": 1}))
    api.send_message(100, "Записал!")
    assert "reply_markup" not in json.loads(requests[0].content)


def test_get_updates_passes_offset_and_returns_result():
    updates = [{"update_id": 7}]
    api, requests = make_api(lambda req, n: ok_response(updates))
    got = api.get_updates(offset=7, timeout=30)
    body = json.loads(requests[0].content)
    assert body["offset"] == 7
    assert body["timeout"] == 30
    assert got == updates


# ── Retry ────────────────────────────────────────────────────────────────────

def test_retries_on_429_then_succeeds():
    def handler(request, call_number):
        if call_number == 1:
            return httpx.Response(429, json={"ok": False, "error_code": 429,
                                             "parameters": {"retry_after": 0}})
        return ok_response({"message_id": 1})

    api, requests = make_api(handler)
    api.send_message(100, "x")
    assert len(requests) == 2


def test_retries_on_5xx_and_network_error():
    def handler(request, call_number):
        if call_number == 1:
            return httpx.Response(502, text="bad gateway")
        if call_number == 2:
            raise httpx.ConnectError("сеть моргнула")
        return ok_response(True)

    api, requests = make_api(handler)
    api.delete_webhook()
    assert len(requests) == 3


def test_retries_exhausted_raises():
    api, requests = make_api(lambda req, n: httpx.Response(502, text="bad gateway"))
    with pytest.raises(TelegramAPIError):
        api.get_me()
    assert len(requests) == 4  # 1 вызов + 3 повтора


def test_api_logic_error_is_not_retried():
    api, requests = make_api(lambda req, n: httpx.Response(
        400, json={"ok": False, "error_code": 400,
                   "description": "Bad Request: chat not found"}))
    with pytest.raises(TelegramAPIError, match="chat not found"):
        api.send_message(100, "x")
    assert len(requests) == 1, "логическая ошибка API — повторять бессмысленно"


def test_blocked_by_user_is_chat_unavailable():
    api, requests = make_api(lambda req, n: httpx.Response(
        403, json={"ok": False, "error_code": 403,
                   "description": "Forbidden: bot was blocked by the user"}))
    with pytest.raises(ChatUnavailableError):
        api.send_message(100, "x")
    assert len(requests) == 1


def test_chat_not_found_is_chat_unavailable():
    api, _ = make_api(lambda req, n: httpx.Response(
        400, json={"ok": False, "error_code": 400,
                   "description": "Bad Request: chat not found"}))
    with pytest.raises(ChatUnavailableError):
        api.send_message(100, "x")


def test_other_logic_error_stays_generic():
    api, _ = make_api(lambda req, n: httpx.Response(
        400, json={"ok": False, "error_code": 400,
                   "description": "Bad Request: message text is empty"}))
    with pytest.raises(TelegramAPIError) as exc:
        api.send_message(100, "x")
    assert not isinstance(exc.value, ChatUnavailableError), \
        "не-чатовая логическая ошибка — обычная (баг нашего кода, не молчать)"
