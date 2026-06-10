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


# ── C-2: setWebhook с подтверждением — сбой не роняет процесс ────────────────

from navbat.telegram.api import TelegramAPIError
from navbat.telegram.transport import ensure_webhook
from test_dialog_booking import RecordingNotifier


class FakeWebhookAPI:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls: list[tuple[str, str]] = []

    def set_webhook(self, url, secret_token):
        self.calls.append((url, secret_token))
        if self.failures:
            self.failures -= 1
            raise TelegramAPIError("bad webhook: HTTPS url must be provided")
        return True


def test_ensure_webhook_success_first_try():
    api = FakeWebhookAPI()
    assert ensure_webhook(api, "https://x.uz/", "s", path="/webhook/abc",
                          waiter=lambda _: None) is True
    assert api.calls == [("https://x.uz/webhook/abc", "s")]


def test_ensure_webhook_retries_then_succeeds():
    api = FakeWebhookAPI(failures=2)
    notifier = RecordingNotifier()
    assert ensure_webhook(api, "https://x.uz", "s", notifier=notifier,
                          path="/webhook/abc", waiter=lambda _: None) is True
    assert len(api.calls) == 3
    assert notifier.calls == []  # успех — алерта нет


def test_ensure_webhook_exhausted_alerts_and_survives():
    api = FakeWebhookAPI(failures=99)
    notifier = RecordingNotifier()
    assert ensure_webhook(api, "https://x.uz", "s", notifier=notifier,
                          path="/webhook/abc", waiter=lambda _: None) is False
    assert len(api.calls) == 3  # WEBHOOK_SETUP_RETRIES
    assert len(notifier.calls) == 1
    assert "webhook" in notifier.calls[0][1].lower()


# ── C-6: push Google Calendar будит синк ─────────────────────────────────────

import threading

from conftest import make_doctor


def _gcal_doctor(admin_engine, clinic_id, channel_id="CH-1"):
    doctor_id = make_doctor(admin_engine, clinic_id)
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE doctor SET gcal_calendar_id = 'cal@x', "
                          "gcal_channel_id = :ch WHERE id = :d"),
                     {"ch": channel_id, "d": doctor_id})


def gcal_post(server, token) -> httpx.Response:
    return httpx.post(f"http://127.0.0.1:{server.port}/gcal/push/{token}",
                      headers={"X-Goog-Resource-State": "exists"})


def test_gcal_push_wakes_sync(app_session_factory, admin_engine, clinic_a):
    _gcal_doctor(admin_engine, clinic_a, channel_id="CH-1")
    wake = threading.Event()
    server = WebhookServer(app_session_factory, clinic_a, secret=SECRET,
                           host="127.0.0.1", port=0, gcal_wake=wake)
    server.start()
    try:
        assert gcal_post(server, "CH-1").status_code == 200
        assert wake.is_set()
    finally:
        server.stop()


def test_gcal_push_unknown_channel_404(app_session_factory, admin_engine, clinic_a):
    _gcal_doctor(admin_engine, clinic_a, channel_id="CH-1")
    wake = threading.Event()
    server = WebhookServer(app_session_factory, clinic_a, secret=SECRET,
                           host="127.0.0.1", port=0, gcal_wake=wake)
    server.start()
    try:
        assert gcal_post(server, "CH-STALE").status_code == 404
        assert not wake.is_set()
    finally:
        server.stop()


def test_gcal_push_without_calendar_404(app_session_factory, admin_engine, clinic_a):
    # календарь выключен: gcal_wake не передан — путь закрыт, telegram живёт
    server = webhook_server(app_session_factory, clinic_a)
    try:
        assert gcal_post(server, "CH-1").status_code == 404
        assert post(server, tg_message(10)).status_code == 200
    finally:
        server.stop()
