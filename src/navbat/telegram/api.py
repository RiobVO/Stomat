"""Тонкий синхронный клиент Telegram Bot API.

Нужны шесть методов — фреймворк (PTB/aiogram, оба asyncio) был бы конфликтом
со синхронным стеком и лишней зависимостью. Retry: 429 (уважаем retry_after),
5xx и сетевые ошибки; логические 4xx не повторяются — бессмысленно.
"""
from __future__ import annotations

import logging
import time
from typing import Sequence

import httpx

from navbat.dialog.replies import Button

log = logging.getLogger("navbat.telegram")

LONG_POLL_TIMEOUT = 30
# httpx-таймаут больше long-poll: сервер держит соединение до timeout секунд
_HTTP_TIMEOUT = httpx.Timeout(LONG_POLL_TIMEOUT + 5, connect=5)
_RETRY_DELAYS = (1, 2, 4)


class TelegramAPIError(Exception):
    """Ответ ok:false либо исчерпанные повторы."""


class TelegramAPI:
    def __init__(
        self,
        token: str,
        client: httpx.Client | None = None,
        retry_delays: Sequence[float] = _RETRY_DELAYS,
    ) -> None:
        self._base = f"https://api.telegram.org/bot{token}"
        self._client = client or httpx.Client(timeout=_HTTP_TIMEOUT)
        self._retry_delays = tuple(retry_delays)

    # ── Методы Bot API ───────────────────────────────────────────────────

    def get_updates(self, offset: int | None = None,
                    timeout: int = LONG_POLL_TIMEOUT) -> list[dict]:
        params: dict = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", **params)

    def send_message(self, chat_id: int, text: str,
                     buttons: Sequence[Button] = (),
                     contact_request: str | None = None,
                     remove_keyboard: bool = False) -> dict:
        params: dict = {"chat_id": chat_id, "text": text}
        if contact_request:
            # one_time_keyboard: клавиатура прячется после нажатия сама
            params["reply_markup"] = {
                "keyboard": [[{"text": contact_request, "request_contact": True}]],
                "resize_keyboard": True,
                "one_time_keyboard": True,
            }
        elif buttons:
            params["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": b.label, "callback_data": b.action}] for b in buttons
                ]
            }
        elif remove_keyboard:
            params["reply_markup"] = {"remove_keyboard": True}
        return self._call("sendMessage", **params)

    def answer_callback_query(self, callback_query_id: str) -> bool:
        return self._call("answerCallbackQuery", callback_query_id=callback_query_id)

    def get_me(self) -> dict:
        return self._call("getMe")

    def set_webhook(self, url: str, secret_token: str) -> bool:
        return self._call("setWebhook", url=url, secret_token=secret_token,
                          allowed_updates=["message", "callback_query"])

    def delete_webhook(self) -> bool:
        return self._call("deleteWebhook")

    # ── Транспорт с повторами ────────────────────────────────────────────

    def _call(self, method: str, **params):
        last_error: Exception | None = None
        for attempt in range(len(self._retry_delays) + 1):
            if attempt:
                time.sleep(self._retry_delays[attempt - 1])
            try:
                response = self._client.post(f"{self._base}/{method}", json=params)
            except httpx.TransportError as e:
                last_error = e
                log.warning("telegram %s: сеть (попытка %d): %s", method, attempt + 1, e)
                continue
            if response.status_code == 429:
                payload = response.json()
                retry_after = payload.get("parameters", {}).get("retry_after", 1)
                last_error = TelegramAPIError(f"429: retry_after={retry_after}")
                log.warning("telegram %s: 429, ждём %s с", method, retry_after)
                time.sleep(retry_after)
                continue
            if response.status_code >= 500:
                last_error = TelegramAPIError(f"{response.status_code}: {response.text[:200]}")
                log.warning("telegram %s: %d (попытка %d)", method,
                            response.status_code, attempt + 1)
                continue
            payload = response.json()
            if not payload.get("ok"):
                # логическая ошибка (chat not found и т.п.) — повтор не поможет
                raise TelegramAPIError(payload.get("description", str(payload)))
            return payload["result"]
        raise TelegramAPIError(f"{method}: повторы исчерпаны: {last_error}")
