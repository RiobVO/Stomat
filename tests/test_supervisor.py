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
