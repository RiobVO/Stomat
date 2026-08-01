"""Сборка приложения канала: реквизиты бота из clinic."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from navbat.crypto import encrypt_text
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


def test_credentials_without_secret_is_none(app_session_factory, admin_engine,
                                            clinic_a):
    with admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE clinic SET tg_bot_token_encrypted = :tok WHERE id = :id"),
            {"tok": encrypt_text("123:token"), "id": clinic_a},
        )
    creds = load_clinic_credentials(app_session_factory, clinic_a)
    assert creds.webhook_secret is None
