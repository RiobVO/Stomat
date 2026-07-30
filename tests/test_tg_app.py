"""Сборка приложения канала: реквизиты бота из clinic."""
from __future__ import annotations

import sys

import pytest
from sqlalchemy import text

from navbat.crypto import encrypt_text
from navbat.nlu.extractor import FakeExtractor
from navbat.telegram import app as app_module
from navbat.telegram.app import load_clinic_credentials


def test_loads_decrypted_token_and_admin_chat(app_session_factory, admin_engine,
                                              clinic_a):
    with admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE clinic SET tg_bot_token_encrypted = :tok, "
                 "tg_admin_chat_ids = ARRAY[777, 888]::bigint[], "
                 "tg_webhook_secret_encrypted = :sec WHERE id = :id"),
            {"tok": encrypt_text("123:ABC"), "sec": encrypt_text("s3cret"),
             "id": clinic_a},
        )
    creds = load_clinic_credentials(app_session_factory, clinic_a)
    assert creds.token == "123:ABC"
    assert creds.admin_chat_ids == (777, 888)  # все админ-чаты (M4)
    assert creds.webhook_secret == "s3cret"  # C-2: хранится шифртекстом


def test_missing_token_is_config_error(app_session_factory, clinic_a):
    with pytest.raises(SystemExit):
        load_clinic_credentials(app_session_factory, clinic_a)


def test_channel_worker_knows_admin_chats(monkeypatch, app_session_factory,
                                          admin_engine, clinic_a):
    """`python -m navbat.telegram` — второй задокументированный вход в канал.

    Воркер собирался тут без списка админ-чатов, хотя реквизиты уже прочитаны:
    авторизация админ-команд идёт по нему, поэтому /pause, /release, /stats и
    вся кнопочная консоль в админ-чате были мертвы, а сообщения владельца
    уходили в пациентский диалог. Тот же класс, что уже чинили в standalone-
    синке календаря: дублирующая сборка точки входа отстала от воркера."""
    with admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE clinic SET tg_bot_token_encrypted = :tok, "
                 "tg_admin_chat_ids = ARRAY[777, 888]::bigint[] WHERE id = :id"),
            {"tok": encrypt_text("123:ABC"), "id": clinic_a})

    built: list[dict] = []

    class StubAPI:
        def __init__(self, token): pass
        def get_me(self): return {"username": "bot"}
        def delete_webhook(self): return True

    class StubWorker:
        def __init__(self, *args, **kwargs): built.append(kwargs)
        def run(self, stop): return None

    class StubTransport:
        def __init__(self, *args, **kwargs): pass
        def run(self, stop): stop.set()

    monkeypatch.setattr(app_module, "TelegramAPI", StubAPI)
    monkeypatch.setattr(app_module, "UpdateWorker", StubWorker)
    monkeypatch.setattr(app_module, "PollingTransport", StubTransport)
    monkeypatch.setattr(app_module, "make_app_engine", lambda: None)
    monkeypatch.setattr(app_module, "make_session_factory",
                        lambda engine: app_session_factory)
    monkeypatch.setattr(app_module, "build_dialog_extractor",
                        lambda *a, **kw: FakeExtractor(script=[]))
    monkeypatch.setattr(sys, "argv",
                        ["navbat.telegram", "--clinic", str(clinic_a),
                         "--workers", "1"])

    assert app_module.main() == 0
    assert built and built[0].get("admin_chat_id") == (777, 888), \
        "воркер канала не знает админ-чатов — админ-команды и консоль мертвы"


def test_credentials_without_secret_is_none(app_session_factory, admin_engine,
                                            clinic_a):
    with admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE clinic SET tg_bot_token_encrypted = :tok WHERE id = :id"),
            {"tok": encrypt_text("123:token"), "id": clinic_a},
        )
    creds = load_clinic_credentials(app_session_factory, clinic_a)
    assert creds.webhook_secret is None
