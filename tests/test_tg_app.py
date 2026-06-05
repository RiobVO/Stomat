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
                 "tg_admin_chat_id = 777, tg_webhook_secret = 's3cret' "
                 "WHERE id = :id"),
            {"tok": encrypt_text("123:ABC"), "id": clinic_a},
        )
    creds = load_clinic_credentials(app_session_factory, clinic_a)
    assert creds.token == "123:ABC"
    assert creds.admin_chat_id == 777
    assert creds.webhook_secret == "s3cret"


def test_missing_token_is_config_error(app_session_factory, clinic_a):
    with pytest.raises(SystemExit):
        load_clinic_credentials(app_session_factory, clinic_a)
