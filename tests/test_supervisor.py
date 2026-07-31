"""Сборка супервизора: разбор офсетов напоминаний."""
from __future__ import annotations

from datetime import timedelta

import pytest

from navbat.supervisor import parse_offsets


def test_default_offsets():
    assert parse_offsets("1440,120") == (timedelta(hours=24), timedelta(hours=2))


def test_demo_offsets_in_minutes():
    assert parse_offsets("2, 1") == (timedelta(minutes=2), timedelta(minutes=1))


@pytest.mark.parametrize("raw", ["", "  ", "abc", "60,abc"])
def test_garbage_rejected(raw):
    with pytest.raises(ValueError):
        parse_offsets(raw)


# ── SIGTERM (C-1): docker stop должен гасить штатно, как Ctrl+C ─────────────

def test_sigterm_handler_sets_stop_event():
    import signal
    import threading

    from navbat.supervisor import install_sigterm_handler

    previous = signal.getsignal(signal.SIGTERM)
    try:
        stop = threading.Event()
        install_sigterm_handler(stop)
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)  # прямой вызов: кросс-платформенно
        assert stop.is_set()
    finally:
        signal.signal(signal.SIGTERM, previous)


# ── env-валидация --real (C-1): dev-ключ и пустые API-ключи недопустимы ─────

def _fresh_key() -> str:
    import base64
    import os as _os
    return base64.b64encode(_os.urandom(32)).decode()


def test_validate_real_env_rejects_dev_enc_key(monkeypatch):
    from navbat.onboard import DEV_ENC_KEY
    from navbat.supervisor import validate_real_env

    monkeypatch.setenv("NAVBAT_ENC_KEY", DEV_ENC_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    problems = validate_real_env()
    assert any("NAVBAT_ENC_KEY" in p for p in problems)


def test_validate_real_env_rejects_missing_enc_key(monkeypatch):
    from navbat.supervisor import validate_real_env

    monkeypatch.delenv("NAVBAT_ENC_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    problems = validate_real_env()
    assert any("NAVBAT_ENC_KEY" in p for p in problems)


def test_validate_real_env_requires_openai_key(monkeypatch):
    from navbat.supervisor import validate_real_env

    monkeypatch.setenv("NAVBAT_ENC_KEY", _fresh_key())
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    problems = validate_real_env()
    assert any("OPENAI_API_KEY" in p for p in problems)


def test_validate_real_env_accepts_prod_config(monkeypatch):
    from navbat.supervisor import validate_real_env

    monkeypatch.setenv("NAVBAT_ENC_KEY", _fresh_key())
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert validate_real_env() == []
