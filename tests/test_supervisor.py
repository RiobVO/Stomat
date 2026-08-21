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


# ── Надзор за потоками: смерть фоновой работы обязана быть замеченной ───────

class _RecordingNotifier:
    def __init__(self) -> None:
        self.system: list[str] = []

    def notify(self, chat_id, reason, context):
        self.system.append(reason)

    def notify_system(self, reason, context):
        self.system.append(reason)


def test_dead_thread_stops_the_process_with_alert():
    """Умерший поток = процесс больше не делает свою работу, но выглядит живым.

    Воркеры, календарь и напоминания живут потоками одного процесса. Если
    поток умрёт, главный поток продолжит крутить polling, docker-healthcheck
    (light-ветка /health) останется зелёным, а очередь никто не разберёт.
    Надзор обязан это заметить: сигнал владельцу системы и остановка процесса,
    чтобы контейнер поднялся заново."""
    import threading

    from navbat.supervisor import supervise_threads

    dead = threading.Thread(target=lambda: None, name="worker-0")
    dead.start()
    dead.join()
    alive = threading.Thread(target=lambda: threading.Event().wait(5),
                             name="reminders", daemon=True)
    alive.start()

    stop = threading.Event()
    notifier = _RecordingNotifier()
    published: list[str] = []
    died = supervise_threads([dead, alive], stop, notifier=notifier,
                             interval=0.01, died=published)

    assert died == ["worker-0"]
    assert published == ["worker-0"], \
        "главный поток проснётся от stop и не узнает об аварии"
    assert stop.is_set(), "процесс не остановлен — бот молчит, но контейнер жив"
    assert notifier.system and "worker-0" in notifier.system[0]
    stop.set()


def test_dead_thread_stops_process_even_if_alert_fails():
    """Недоставленный алерт не отменяет остановку: молча работающий процесс
    хуже, чем процесс без уведомления."""
    import threading

    from navbat.supervisor import supervise_threads

    class _BrokenNotifier:
        def notify_system(self, reason, context):
            raise RuntimeError("телеграм недоступен")

    dead = threading.Thread(target=lambda: None, name="calendar")
    dead.start()
    dead.join()
    stop = threading.Event()
    published: list[str] = []

    assert supervise_threads([dead], stop, notifier=_BrokenNotifier(),
                             interval=0.01, died=published) == ["calendar"]
    assert stop.is_set() and published == ["calendar"]


def test_supervisor_watch_is_quiet_until_shutdown():
    """Штатная остановка (Ctrl+C, SIGTERM) — не смерть потока: без алертов."""
    import threading

    from navbat.supervisor import supervise_threads

    alive = threading.Thread(target=lambda: threading.Event().wait(5),
                             name="worker-0", daemon=True)
    alive.start()
    stop = threading.Event()
    notifier = _RecordingNotifier()

    threading.Timer(0.05, stop.set).start()
    assert supervise_threads([alive], stop, notifier=notifier,
                             interval=0.01) == []
    assert notifier.system == []
