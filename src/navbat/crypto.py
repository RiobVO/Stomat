"""AES-256-GCM для PII (имя пациента) — BRIEF требует AES-256.

Ключ — env NAVBAT_ENC_KEY: base64 от 32 байт. Никаких дефолтов:
отсутствие ключа — ошибка конфигурации, не тихий плейнтекст.
"""
from __future__ import annotations

import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_LEN = 12  # стандарт GCM


def _key(override: str | None = None) -> bytes:
    raw = override or os.environ.get("NAVBAT_ENC_KEY")
    if not raw:
        raise RuntimeError("NAVBAT_ENC_KEY не задан (base64 от 32 байт)")
    key = base64.b64decode(raw)
    if len(key) != 32:
        raise RuntimeError("NAVBAT_ENC_KEY: ожидается base64 от 32 байт")
    return key


def encrypt_text(plaintext: str, *, key: str | None = None) -> str:
    """key — явный ключ для ротации (rotate_key); None — env NAVBAT_ENC_KEY."""
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = AESGCM(_key(key)).encrypt(nonce, plaintext.encode(), None)
    return base64.b64encode(nonce + ciphertext).decode()


def decrypt_text(token: str, *, key: str | None = None) -> str:
    try:
        raw = base64.b64decode(token, validate=True)
        nonce, ciphertext = raw[:_NONCE_LEN], raw[_NONCE_LEN:]
        return AESGCM(_key(key)).decrypt(nonce, ciphertext, None).decode()
    except (binascii.Error, InvalidTag, ValueError) as e:
        raise ValueError("повреждённый шифртекст") from e
