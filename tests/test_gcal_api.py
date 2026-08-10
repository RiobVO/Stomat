"""Тонкий клиент Google Calendar: OAuth-refresh, syncToken, retry.

Сеть мокается httpx.MockTransport — реальный Google не дёргается.
"""
from __future__ import annotations

import json

import httpx
import pytest

from navbat.calendar.api import (
    CalendarAuthError,
    GoogleCalendarAPI,
    ResyncRequired,
)

CAL = "doctor-cal@group.calendar.google.com"


def token_response() -> httpx.Response:
    return httpx.Response(200, json={"access_token": "ACCESS", "expires_in": 3600})


def make_api(handler) -> tuple[GoogleCalendarAPI, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return handler_token(request, requests)
        return handler(request, requests)

    def handler_token(request, reqs):
        return token_response()

    client = httpx.Client(transport=httpx.MockTransport(recording))
    api = GoogleCalendarAPI("REFRESH", client_id="CID", client_secret="CSECRET",
                            client=client, retry_delays=(0, 0, 0))
    return api, requests


def api_calls(requests) -> list[httpx.Request]:
    return [r for r in requests if r.url.host != "oauth2.googleapis.com"]


# ── OAuth ────────────────────────────────────────────────────────────────────

def test_first_call_refreshes_token_and_sends_bearer():
    api, requests = make_api(
        lambda req, reqs: httpx.Response(200, json={"items": [], "nextSyncToken": "T"}))
    api.list_events(CAL)

    assert requests[0].url.host == "oauth2.googleapis.com"
    body = dict(pair.split("=") for pair in requests[0].content.decode().split("&"))
    assert body["grant_type"] == "refresh_token"
    assert body["refresh_token"] == "REFRESH"
    assert api_calls(requests)[0].headers["authorization"] == "Bearer ACCESS"


def test_401_triggers_refresh_and_retry():
    state = {"first": True}

    def handler(request, reqs):
        if state.pop("first", False):
            return httpx.Response(401, json={"error": "invalid_credentials"})
        return httpx.Response(200, json={"items": [], "nextSyncToken": "T"})

    api, requests = make_api(handler)
    api.list_events(CAL)
    token_calls = [r for r in requests if r.url.host == "oauth2.googleapis.com"]
    assert len(token_calls) == 2, "после 401 токен обновлён повторно"
    assert len(api_calls(requests)) == 2


def test_failed_refresh_raises_auth_error():
    def recording(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    client = httpx.Client(transport=httpx.MockTransport(recording))
    api = GoogleCalendarAPI("REVOKED", client_id="CID", client_secret="CSECRET",
                            client=client, retry_delays=(0,))
    with pytest.raises(CalendarAuthError):
        api.list_events(CAL)


# ── list_events ──────────────────────────────────────────────────────────────

# ── watch-каналы (C-6) ───────────────────────────────────────────────────────

def test_watch_events_posts_channel():
    api, requests = make_api(
        lambda req, reqs: httpx.Response(200, json={
            "resourceId": "RES1", "expiration": "1760000000000"}))
    result = api.watch_events(CAL, "CH-1", "https://x.uz/gcal/push/CH-1")

    call = api_calls(requests)[0]
    assert call.url.path.endswith("/events/watch")
    body = json.loads(call.content)
    assert body == {"id": "CH-1", "type": "web_hook",
                    "address": "https://x.uz/gcal/push/CH-1"}
    assert result["resourceId"] == "RES1"


def test_stop_channel_missing_is_ok():
    # канал уже умер у Google (404) — идемпотентность важнее строгости
    api, requests = make_api(lambda req, reqs: httpx.Response(404, json={}))
    api.stop_channel("CH-1", "RES1")
    call = api_calls(requests)[0]
    assert call.url.path.endswith("/channels/stop")
    assert json.loads(call.content) == {"id": "CH-1", "resourceId": "RES1"}


def test_list_events_paginates_and_returns_sync_token():
    def handler(request, reqs):
        params = dict(request.url.params)
        if "pageToken" not in params:
            return httpx.Response(200, json={"items": [{"id": "e1"}],
                                             "nextPageToken": "PAGE2"})
        return httpx.Response(200, json={"items": [{"id": "e2"}],
                                         "nextSyncToken": "SYNC"})

    api, requests = make_api(handler)
    events, sync_token = api.list_events(CAL, sync_token="OLD")

    assert [e["id"] for e in events] == ["e1", "e2"]
    assert sync_token == "SYNC"
    first = dict(api_calls(requests)[0].url.params)
    assert first["syncToken"] == "OLD"
    assert first["singleEvents"] == "true"
    assert first["showDeleted"] == "true"


def test_gone_sync_token_requires_resync():
    api, _ = make_api(lambda req, reqs: httpx.Response(410, json={}))
    with pytest.raises(ResyncRequired):
        api.list_events(CAL, sync_token="STALE")


# ── Мутации ──────────────────────────────────────────────────────────────────

def test_insert_patch_delete_requests():
    api, requests = make_api(lambda req, reqs: httpx.Response(200, json={"id": "EV"}))
    api.insert_event(CAL, {"summary": "Чистка"})
    api.patch_event(CAL, "EV", {"start": {"dateTime": "2026-06-08T09:00:00+05:00"}})
    api.delete_event(CAL, "EV")

    insert, patch, delete = api_calls(requests)
    assert insert.method == "POST" and insert.url.path.endswith("/events")
    assert json.loads(insert.content)["summary"] == "Чистка"
    assert patch.method == "PATCH" and patch.url.path.endswith("/events/EV")
    assert delete.method == "DELETE"


def test_delete_missing_event_is_ok():
    api, _ = make_api(lambda req, reqs: httpx.Response(404, json={}))
    api.delete_event(CAL, "GONE")  # идемпотентность: уже удалено — не ошибка


def test_delete_gone_event_is_ok():
    # Google v3 отдаёт 410 Gone на DELETE уже-удалённого/вручную снятого события
    # (не 404) — с missing_ok это успех, а не ResyncRequired (иначе синк врача
    # залипает в вечном сбое: событие пере-выбирается _export каждый цикл)
    api, _ = make_api(lambda req, reqs: httpx.Response(410, json={}))
    api.delete_event(CAL, "GONE")  # не должно бросать ResyncRequired


def test_stop_channel_gone_is_ok():
    # просроченный watch-канал у Google часто отдаёт 410 — идемпотентно, не resync
    api, _ = make_api(lambda req, reqs: httpx.Response(410, json={}))
    api.stop_channel("CH-1", "RES1")  # не должно бросать ResyncRequired


# ── free_busy и retry ────────────────────────────────────────────────────────

def test_free_busy_detects_overlap():
    def handler(request, reqs):
        body = json.loads(request.content)
        assert body["items"] == [{"id": CAL}]
        return httpx.Response(200, json={
            "calendars": {CAL: {"busy": [{"start": "x", "end": "y"}]}}})

    api, _ = make_api(handler)
    assert api.free_busy(CAL, "2026-06-08T09:00:00Z", "2026-06-08T09:30:00Z") is True


def test_free_busy_empty_means_free():
    api, _ = make_api(lambda req, reqs: httpx.Response(
        200, json={"calendars": {CAL: {"busy": []}}}))
    assert api.free_busy(CAL, "a", "b") is False


def test_retries_on_5xx():
    state = {"n": 0}

    def handler(request, reqs):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(503, text="backend error")
        return httpx.Response(200, json={"id": "EV"})

    api, requests = make_api(handler)
    api.insert_event(CAL, {})
    assert len(api_calls(requests)) == 2
