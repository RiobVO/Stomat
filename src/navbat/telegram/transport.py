"""Транспорты доставки апдейтов: long polling и webhook.

Оба только складывают апдейты в durable-очередь (queue.enqueue) — обработка
строго в воркерах. Дедуп решает UNIQUE(clinic_id, update_id): рестарт поллера
или повтор webhook безвредны, поэтому offset поллинга живёт в памяти.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sqlalchemy.orm import Session, sessionmaker

from navbat.db.base import tenant_transaction
from navbat.telegram.api import LONG_POLL_TIMEOUT, TelegramAPIError
from navbat.telegram.queue import enqueue

log = logging.getLogger("navbat.telegram")

POLL_ERROR_WAIT = 5.0  # сек паузы после сбоя getUpdates

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


def _chat_of(update: dict) -> int:
    if "message" in update:
        return update["message"]["chat"]["id"]
    if "callback_query" in update:
        return update["callback_query"]["message"]["chat"]["id"]
    return 0  # служебный тип без чата (воркер закроет молча)


class PollingTransport:
    def __init__(self, session_factory: sessionmaker[Session],
                 clinic_id: uuid.UUID, api) -> None:
        self._session_factory = session_factory
        self._clinic_id = clinic_id
        self._api = api
        self._offset: int | None = None

    def poll_once(self, timeout: int = LONG_POLL_TIMEOUT) -> int:
        updates = self._api.get_updates(offset=self._offset, timeout=timeout)
        for update in updates:
            with tenant_transaction(self._session_factory, self._clinic_id) as session:
                enqueue(session, update["update_id"], _chat_of(update), update)
            self._offset = update["update_id"] + 1
        return len(updates)

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.poll_once()
            except TelegramAPIError as e:
                log.error("polling: %s — пауза %s с", e, POLL_ERROR_WAIT)
                stop.wait(POLL_ERROR_WAIT)


class WebhookServer:
    """Мгновенный 200: хендлер делает один INSERT в очередь и отвечает.

    TLS терминирует nginx (деплой-инкремент); подлинность запросов —
    секрет-заголовок Telegram (setWebhook secret_token).
    """

    def __init__(self, session_factory: sessionmaker[Session],
                 clinic_id: uuid.UUID, secret: str,
                 host: str = "0.0.0.0", port: int = 8443,
                 path: str | None = None) -> None:
        self.path = path or f"/webhook/{clinic_id}"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — API stdlib
                # тело вычитывается ДО любого ответа: отказ (403/404) с
                # непрочитанным телом рвёт сокет на полуслове (WinError 10053)
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path != outer.path:
                    self._respond(404)
                    return
                if self.headers.get(SECRET_HEADER) != secret:
                    self._respond(403)
                    return
                try:
                    update = json.loads(body)
                    with tenant_transaction(outer._session_factory,
                                            outer._clinic_id) as session:
                        enqueue(session, update["update_id"], _chat_of(update), update)
                except Exception:
                    log.exception("webhook: апдейт не принят")
                    self._respond(500)  # Telegram повторит доставку
                    return
                self._respond(200)

            def _respond(self, code: int) -> None:
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, fmt: str, *args) -> None:
                log.debug("webhook: " + fmt, *args)

        self._session_factory = session_factory
        self._clinic_id = clinic_id
        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="webhook", daemon=True)
        self._thread.start()
        log.info("webhook-сервер слушает :%d%s", self.port, self.path)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)


WEBHOOK_SETUP_RETRIES = 3
WEBHOOK_SETUP_BACKOFF = (2.0, 5.0)  # паузы между попытками, сек


def ensure_webhook(api, url: str, secret: str, notifier=None, path: str = "",
                   waiter=time.sleep) -> bool:
    """setWebhook с подтверждением: сбой — алерт, не падение процесса.

    TelegramAPI._call сам ретраит сеть/5xx/429; здесь добиваем логические
    отказы (кривой URL, битый cert) и шлём алерт после исчерпания —
    nginx/certbot могут подняться позже, процесс должен жить.
    """
    full_url = url.rstrip("/") + path
    for attempt in range(WEBHOOK_SETUP_RETRIES):
        try:
            api.set_webhook(full_url, secret_token=secret)
            log.info("webhook установлен: %s", full_url)
            return True
        except TelegramAPIError as e:
            log.error("setWebhook (попытка %d/%d): %s",
                      attempt + 1, WEBHOOK_SETUP_RETRIES, e)
            if attempt < WEBHOOK_SETUP_RETRIES - 1:
                waiter(WEBHOOK_SETUP_BACKOFF[
                    min(attempt, len(WEBHOOK_SETUP_BACKOFF) - 1)])
    if notifier is not None:
        from navbat.dialog.escalation import system_alert

        system_alert(
            notifier,
            f"webhook не установлен после {WEBHOOK_SETUP_RETRIES} попыток — "
            f"бот глух для Telegram. Проверьте домен/cert. URL: {full_url}",
            {})
    return False
