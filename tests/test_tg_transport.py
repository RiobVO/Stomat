"""Транспорты: long polling и webhook-сервер. Оба пишут в одну очередь.

Webhook тестируется живым HTTP на localhost (эфемерный порт);
Telegram API мокается фейком — сетевых вызовов наружу нет.
"""
from __future__ import annotations

import httpx
from sqlalchemy import text

from navbat.telegram.transport import PollingTransport, WebhookServer

CHAT = 100
SECRET = "test-secret"


def queue_update_ids(admin_engine) -> list[int]:
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT update_id FROM message_queue ORDER BY update_id")
        ).scalars().all()


def tg_message(update_id: int, chat_id: int = CHAT) -> dict:
    return {"update_id": update_id,
            "message": {"chat": {"id": chat_id}, "text": "salom"}}


# ── Polling ──────────────────────────────────────────────────────────────────

class FakePollingAPI:
    def __init__(self, batches: list[list[dict]]) -> None:
        self.batches = list(batches)
        self.offsets: list[int | None] = []

    def get_updates(self, offset=None, timeout=0):
        self.offsets.append(offset)
        return self.batches.pop(0) if self.batches else []


def test_polling_enqueues_and_advances_offset(app_session_factory, admin_engine,
                                              clinic_a):
    api = FakePollingAPI([[tg_message(10), tg_message(11)], [tg_message(12)]])
    transport = PollingTransport(app_session_factory, clinic_a, api)

    assert transport.poll_once(timeout=0) == 2
    assert transport.poll_once(timeout=0) == 1
    assert queue_update_ids(admin_engine) == [10, 11, 12]
    # offset подтверждает только принятое: последний update_id + 1
    assert api.offsets == [None, 12]


def test_polling_duplicates_are_deduped(app_session_factory, admin_engine, clinic_a):
    # рестарт поллера: getUpdates вернул уже виденный апдейт
    api = FakePollingAPI([[tg_message(10)], [tg_message(10), tg_message(11)]])
    transport = PollingTransport(app_session_factory, clinic_a, api)
    transport.poll_once(timeout=0)
    transport.poll_once(timeout=0)
    assert queue_update_ids(admin_engine) == [10, 11]


# ── Webhook ──────────────────────────────────────────────────────────────────

def webhook_server(app_session_factory, clinic_id):
    server = WebhookServer(app_session_factory, clinic_id, secret=SECRET,
                           host="127.0.0.1", port=0)
    server.start()
    return server


def post(server, payload, secret=SECRET, path=None) -> httpx.Response:
    url = f"http://127.0.0.1:{server.port}{path or server.path}"
    headers = {}
    if secret is not None:
        headers["X-Telegram-Bot-Api-Secret-Token"] = secret
    return httpx.post(url, json=payload, headers=headers)


def test_webhook_accepts_update_instantly(app_session_factory, admin_engine, clinic_a):
    server = webhook_server(app_session_factory, clinic_a)
    try:
        response = post(server, tg_message(10))
        assert response.status_code == 200
        assert queue_update_ids(admin_engine) == [10]
    finally:
        server.stop()


def test_webhook_duplicate_gives_single_row(app_session_factory, admin_engine, clinic_a):
    server = webhook_server(app_session_factory, clinic_a)
    try:
        assert post(server, tg_message(10)).status_code == 200
        assert post(server, tg_message(10)).status_code == 200
        assert queue_update_ids(admin_engine) == [10]
    finally:
        server.stop()


def test_webhook_rejects_bad_secret(app_session_factory, admin_engine, clinic_a):
    server = webhook_server(app_session_factory, clinic_a)
    try:
        assert post(server, tg_message(10), secret="wrong").status_code == 403
        assert post(server, tg_message(10), secret=None).status_code == 403
        assert queue_update_ids(admin_engine) == []
    finally:
        server.stop()


def test_webhook_unknown_path_is_404(app_session_factory, admin_engine, clinic_a):
    server = webhook_server(app_session_factory, clinic_a)
    try:
        assert post(server, tg_message(10), path="/webhook/other").status_code == 404
        assert queue_update_ids(admin_engine) == []
    finally:
        server.stop()
