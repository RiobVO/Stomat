"""Онбординг Google-аккаунта клиники: loopback OAuth → refresh token в clinic.

    python -m navbat.calendar.auth --clinic <uuid> [--port 8765]

Открывает браузер на consent-странице Google; локальный HTTP-сервер ловит
authorization code, обменивает на refresh token и сохраняет шифртекстом.
Требует NAVBAT_GCAL_CLIENT_ID / NAVBAT_GCAL_CLIENT_SECRET / NAVBAT_ENC_KEY.
"""
from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys
import threading
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from sqlalchemy import text

from navbat.calendar.api import TOKEN_URL, CalendarAuthError
from navbat.crypto import encrypt_text
from navbat.db.base import make_app_engine, make_session_factory, tenant_transaction

log = logging.getLogger("navbat.calendar")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPE = "https://www.googleapis.com/auth/calendar"


def exchange_code(client: httpx.Client, code: str, client_id: str,
                  client_secret: str, redirect_uri: str) -> str:
    response = client.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    })
    if response.status_code != 200:
        raise CalendarAuthError(
            f"обмен кода не удался ({response.status_code}): {response.text[:200]}")
    refresh_token = response.json().get("refresh_token")
    if not refresh_token:
        # Google отдаёт refresh_token только при prompt=consent + access_type=offline
        raise CalendarAuthError("ответ без refresh_token — повторите с отзывом "
                                "доступа приложению в аккаунте Google")
    return refresh_token


def _make_handler(received: dict, got_code: threading.Event,
                  expected_state: str) -> type:
    """Фабрика обработчика loopback-callback'а (вынесена для тестируемости)."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — API stdlib
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = (query.get("code") or [None])[0]
            state = (query.get("state") or [None])[0]
            # код фиксируем один раз и ТОЛЬКО при совпадении state (анти-CSRF):
            # браузер следом просит /favicon.ico (GET без ?code= не должен
            # затереть пойманный код — гонка давала «Missing parameter: code»),
            # а чужой/пустой state — подделка callback'а, игнорируем
            if code and state == expected_state and "code" not in received:
                received["code"] = code
                got_code.set()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Готово — вернитесь в консоль.".encode())

        def log_message(self, fmt, *args) -> None:
            log.debug("oauth-callback: " + fmt, *args)

    return Handler


def run_loopback_flow(clinic_id: uuid.UUID, port: int) -> None:
    client_id = os.environ.get("NAVBAT_GCAL_CLIENT_ID")
    client_secret = os.environ.get("NAVBAT_GCAL_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit("[FAIL] нужны NAVBAT_GCAL_CLIENT_ID и NAVBAT_GCAL_CLIENT_SECRET")

    redirect_uri = f"http://localhost:{port}/"
    state = secrets.token_urlsafe(24)  # анти-CSRF: сверяем в callback
    received: dict = {}
    got_code = threading.Event()

    server = ThreadingHTTPServer(("localhost", port),
                                 _make_handler(received, got_code, state))
    threading.Thread(target=server.serve_forever, daemon=True).start()

    consent_url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",  # иначе при повторной авторизации refresh_token не придёт
        "state": state,
    })
    print(f"Открываю браузер для авторизации Google…\n{consent_url}")
    webbrowser.open(consent_url)
    try:
        if not got_code.wait(timeout=300) or not received.get("code"):
            sys.exit("[FAIL] код авторизации не получен за 5 минут")
    finally:
        server.shutdown()
        server.server_close()

    with httpx.Client(timeout=15) as client:
        refresh_token = exchange_code(client, received["code"], client_id,
                                      client_secret, redirect_uri)
    session_factory = make_session_factory(make_app_engine())
    with tenant_transaction(session_factory, clinic_id) as session:
        session.execute(
            text("UPDATE clinic SET gcal_refresh_token_encrypted = :token "
                 "WHERE id = :id"),
            {"token": encrypt_text(refresh_token), "id": clinic_id},
        )
    print(f"[OK] refresh token сохранён для клиники {clinic_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Онбординг Google Calendar")
    parser.add_argument("--clinic", required=True, type=uuid.UUID)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not os.environ.get("NAVBAT_ENC_KEY"):
        sys.exit("[FAIL] NAVBAT_ENC_KEY не задан")
    run_loopback_flow(args.clinic, args.port)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
