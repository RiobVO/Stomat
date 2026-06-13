"""Loopback-обработчик OAuth: код фиксируется один раз и не теряется.

Гонка из жизни: браузер после redirect'а с ?code= тут же просит /favicon.ico —
GET без code затирал пойманный код на None, Google отвечал 400
«Missing required parameter: code». БД тесту не нужна.
"""
from __future__ import annotations

import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from navbat.calendar.auth import _make_handler

STATE = "expected-state-token"


@pytest.fixture
def loopback():
    received: dict = {}
    got_code = threading.Event()
    server = ThreadingHTTPServer(
        ("localhost", 0), _make_handler(received, got_code, STATE))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://localhost:{server.server_address[1]}"
    yield base, received, got_code
    server.shutdown()
    server.server_close()


def _get(url: str) -> None:
    with urllib.request.urlopen(url, timeout=5) as resp:
        resp.read()


def test_favicon_after_code_does_not_clobber_code(loopback):
    base, received, got_code = loopback
    _get(base + f"/?code=abc&state={STATE}")
    _get(base + "/favicon.ico")
    assert received["code"] == "abc"
    assert got_code.is_set()


def test_request_without_code_first_then_code_is_captured(loopback):
    base, received, got_code = loopback
    _get(base + "/favicon.ico")
    assert not got_code.is_set()
    assert "code" not in received
    _get(base + f"/?code=xyz&state={STATE}")
    assert received["code"] == "xyz"
    assert got_code.is_set()


def test_callback_with_wrong_or_missing_state_is_rejected(loopback):
    # анти-CSRF: код с чужим или отсутствующим state не фиксируется
    base, received, got_code = loopback
    _get(base + "/?code=evil&state=attacker")
    _get(base + "/?code=evil2")  # без state
    assert "code" not in received
    assert not got_code.is_set()
    # настоящий редирект с верным state — фиксируется
    _get(base + f"/?code=good&state={STATE}")
    assert received["code"] == "good"
    assert got_code.is_set()
