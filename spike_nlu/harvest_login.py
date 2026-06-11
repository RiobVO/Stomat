"""Неинтерактивный логин харвестера (префикс `!` не даёт stdin для input()).

Двухшагово, всё через аргументы:
    python harvest_login.py request --phone "+998901234567"   # шлёт код в TG
    # код приходит в приложение Telegram (чат «Telegram»)
    python harvest_login.py code 12345 [--password 2FA]       # завершает вход

Создаёт .harvest.session — дальше harvest_telegram.py работает без логина.
phone_code_hash между шагами хранится в .harvest_login.json (gitignored).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from harvest_telegram import SESSION, load_root_env  # переиспускаем

BASE = Path(__file__).parent
STATE = BASE / ".harvest_login.json"


def _client() -> TelegramClient:
    import os

    return TelegramClient(SESSION, int(os.environ["TG_API_ID"]),
                          os.environ["TG_API_HASH"])


async def do_request(phone: str) -> int:
    client = _client()
    await client.connect()
    if await client.is_user_authorized():
        print("[OK] уже залогинен — шаг code не нужен")
        await client.disconnect()
        return 0
    sent = await client.send_code_request(phone)
    STATE.write_text(json.dumps(
        {"phone": phone, "hash": sent.phone_code_hash}), encoding="utf-8")
    await client.disconnect()
    print(f"[OK] код отправлен на {phone} (придёт в чат «Telegram»). "
          f"Дальше: python harvest_login.py code <КОД>")
    return 0


async def do_code(code: str, password: str | None) -> int:
    if not STATE.exists():
        print("[FAIL] сначала request --phone")
        return 1
    state = json.loads(STATE.read_text(encoding="utf-8"))
    client = _client()
    await client.connect()
    try:
        await client.sign_in(phone=state["phone"], code=code.strip(),
                             phone_code_hash=state["hash"])
    except SessionPasswordNeededError:
        if not password:
            print("[FAIL] включён 2FA — повтори: code <КОД> --password <2FA>")
            await client.disconnect()
            return 1
        await client.sign_in(password=password)
    me = await client.get_me()
    await client.disconnect()
    STATE.unlink(missing_ok=True)
    print(f"[OK] вход выполнен: {me.first_name} (@{me.username or '—'}). "
          f"Сессия сохранена — теперь запускай harvest_telegram.py")
    return 0


def main() -> int:
    load_root_env()
    parser = argparse.ArgumentParser(description="Неинтерактивный логин харвестера")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_req = sub.add_parser("request")
    p_req.add_argument("--phone", required=True)
    p_code = sub.add_parser("code")
    p_code.add_argument("code")
    p_code.add_argument("--password", default=None)
    args = parser.parse_args()
    if args.cmd == "request":
        return asyncio.run(do_request(args.phone))
    return asyncio.run(do_code(args.code, args.password))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
