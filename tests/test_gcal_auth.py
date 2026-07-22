"""Онбординг Google: обмен authorization code → refresh token, загрузка реквизитов."""
from __future__ import annotations

import httpx
import pytest
from sqlalchemy import text

from navbat.calendar.api import CalendarAuthError
from navbat.calendar.auth import exchange_code
from navbat.calendar.__main__ import load_refresh_token
from navbat.crypto import encrypt_text


def client_with(response: httpx.Response) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda req: response))


def test_exchange_code_returns_refresh_token():
    client = client_with(httpx.Response(200, json={
        "access_token": "A", "refresh_token": "REFRESH", "expires_in": 3599}))
    token = exchange_code(client, code="CODE", client_id="CID",
                          client_secret="S", redirect_uri="http://localhost:1/")
    assert token == "REFRESH"


def test_exchange_without_refresh_token_fails():
    # повторная авторизация без prompt=consent: Google не отдаёт refresh_token
    client = client_with(httpx.Response(200, json={"access_token": "A"}))
    with pytest.raises(CalendarAuthError, match="refresh_token"):
        exchange_code(client, code="CODE", client_id="CID",
                      client_secret="S", redirect_uri="http://localhost:1/")


def test_exchange_error_raises():
    client = client_with(httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(CalendarAuthError):
        exchange_code(client, code="BAD", client_id="CID",
                      client_secret="S", redirect_uri="http://localhost:1/")


# ── Загрузка реквизитов клиники ──────────────────────────────────────────────

def test_load_refresh_token_decrypts(app_session_factory, admin_engine, clinic_a):
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE clinic SET gcal_refresh_token_encrypted = :t "
                          "WHERE id = :id"),
                     {"t": encrypt_text("REFRESH"), "id": clinic_a})
    assert load_refresh_token(app_session_factory, clinic_a) == "REFRESH"


def test_load_refresh_token_missing_is_config_error(app_session_factory, clinic_a):
    with pytest.raises(SystemExit):
        load_refresh_token(app_session_factory, clinic_a)
