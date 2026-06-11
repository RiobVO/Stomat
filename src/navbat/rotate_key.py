"""Ротация NAVBAT_ENC_KEY — перешифровка всех AES-полей новым ключом.

Когда: ключ скомпрометирован (утёк .env или бэкап вместе с ключом).
Процедура (подробно в docs/OPERATIONS.md): остановить app → прогнать
ротацию → заменить NAVBAT_ENC_KEY на новый → поднять app.

    NAVBAT_ENC_KEY_NEW=<base64 от 32 байт> python -m navbat.rotate_key

Старый ключ — текущий NAVBAT_ENC_KEY. Идёт под admin-DSN мимо RLS (ключ
один на все клиники) ОДНОЙ транзакцией: сбой на любом значении — база
не изменена. Повторный прогон безопасен: значения, уже шифрованные
новым ключом, распознаются и пропускаются.
"""
from __future__ import annotations

import logging
import os
import sys

from sqlalchemy import create_engine, text

from navbat.crypto import decrypt_text, encrypt_text
from navbat.envfile import load_env_file

log = logging.getLogger("navbat.rotate_key")

ENCRYPTED_COLUMNS = (
    ("clinic", "tg_bot_token_encrypted"),
    ("clinic", "tg_webhook_secret_encrypted"),
    ("clinic", "gcal_refresh_token_encrypted"),
    ("doctor", "name_encrypted"),
    ("patient", "name_encrypted"),
    ("patient", "phone_encrypted"),
)


def rotate(conn, old_key: str, new_key: str) -> dict[str, int]:
    """Перешифровать все непустые значения; счётчики по колонкам.

    ValueError — значение не читается ни одним ключом (порча данных);
    вызывающий держит транзакцию, исключение откатывает всё целиком."""
    counts: dict[str, int] = {}
    for table, column in ENCRYPTED_COLUMNS:  # имена из константы, не ввод
        rows = conn.execute(text(
            f"SELECT id, {column} AS value FROM {table} "
            f"WHERE {column} IS NOT NULL")).all()
        rotated = 0
        for row in rows:
            try:
                plaintext = decrypt_text(row.value, key=old_key)
            except ValueError:
                try:
                    decrypt_text(row.value, key=new_key)
                except ValueError:
                    raise ValueError(
                        f"{table}.{column} id={row.id}: не читается ни старым, "
                        f"ни новым ключом — порча данных, ротация прервана"
                    ) from None
                continue  # уже новым ключом (повторный прогон)
            conn.execute(text(
                f"UPDATE {table} SET {column} = :value WHERE id = :id"),
                {"value": encrypt_text(plaintext, key=new_key), "id": row.id})
            rotated += 1
        counts[f"{table}.{column}"] = rotated
    return counts


def main() -> int:
    load_env_file()
    old_key = os.environ.get("NAVBAT_ENC_KEY")
    new_key = os.environ.get("NAVBAT_ENC_KEY_NEW")
    if not old_key or not new_key:
        print("[FAIL] нужны NAVBAT_ENC_KEY (старый) и NAVBAT_ENC_KEY_NEW (новый)")
        return 1
    if old_key == new_key:
        print("[FAIL] новый ключ совпадает со старым")
        return 1
    dsn = os.environ.get("NAVBAT_ADMIN_DSN")
    if not dsn:
        print("[FAIL] NAVBAT_ADMIN_DSN не задан (ротация идёт мимо RLS)")
        return 1
    engine = create_engine(dsn)
    try:
        with engine.begin() as conn:
            counts = rotate(conn, old_key, new_key)
    except (ValueError, RuntimeError) as e:
        print(f"[FAIL] {e} — база НЕ изменена (транзакция откатилась)")
        return 1
    finally:
        engine.dispose()
    for name, rotated in counts.items():
        print(f"[OK] {name}: перешифровано {rotated}")
    print("[OK] ротация завершена — замени NAVBAT_ENC_KEY на новый и подними app")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
