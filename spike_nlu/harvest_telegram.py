"""Харвестер живой стоматологической речи из ПУБЛИЧНЫХ Telegram-источников.

Зачем: data flywheel — аутентичные формулировки пациентов («tishim
og'riyapti, narxi qancha») для разметки и fine-tuning NLU. Читаем только
публичные группы и комментарии к постам каналов клиник, малыми объёмами,
с паузами и обработкой FloodWait. Отправитель НЕ сохраняется — только
обезличенный текст.

Подготовка (один раз):
    pip install -r requirements-harvest.txt
    my.telegram.org → API development tools → api_id/api_hash
    в корневой .env: TG_API_ID=..., TG_API_HASH=...
    первый запуск спросит телефон и код из Telegram (session-файл
    .harvest.session остаётся локально, в git не попадает)

Запуск:
    python harvest_telegram.py --discover "stomatologiya"   # поиск кандидатов
    python harvest_telegram.py                              # харвест по конфигу
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.functions.contacts import SearchRequest

BASE = Path(__file__).parent
SESSION = str(BASE / ".harvest")
SEEN_PATH = BASE / "data" / "inbox" / ".seen_hashes"

MIN_LEN, MAX_LEN = 8, 400
PAUSE_BETWEEN_SOURCES = 30  # сек — не злить рейт-лимиты

# телефоны/упоминания/ссылки → плейсхолдеры (та же граница, что в проде)
PHONE_RE = re.compile(r"\+?\d(?:[\s\-()]?\d){6,14}")
MENTION_RE = re.compile(r"@\w{3,}")
LINK_RE = re.compile(r"(?:https?://|t\.me/)\S+", re.IGNORECASE)
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F02F]")

# рекламные маркеры: акции/скидки — это посты клиник, не речь пациентов
PROMO_RE = re.compile(
    r"skidka|скидк|aksiya|акци[яи]|chegirma|чегирма|до\s*-?\d+\s*%|\d+\s*%",
    re.IGNORECASE)

# посты самой клиники (маркетинг/отзывы-врезки) — НЕ речь пациента: рамки
# «Bemorlarimiz / Xulosa / fikri», декоративные эмодзи-заголовки, призывы
CLINIC_POST_RE = re.compile(
    r"bemorlarimiz|xulosa\s*:|fikri\s*:|aziz\s+(do'st|mijoz)|"
    r"manzil\s*:|telefon\s*:|ro'yxatdan\s+o'ting|qo'ng'iroq\s+qiling|"
    r"bizning\s+klinika|biz\s+bilan\s+bog'lan",
    re.IGNORECASE)

# стоматологическая лексика: стемы uz-латиница / uz-кириллица / ru
DENTAL_STEMS = (
    "tish", "plomba", "breket", "implant", "stomatolog", "qoplama",
    "koronka", "rentgen", "ortodont", "karies", "oqartir", "og'ri", "ogri",
    "тиш", "пломб", "брекет", "имплант", "стоматолог", "қоплама", "коронк",
    "рентген", "ортодонт", "кариес", "оқартир", "оғри",
    "зуб", "десн", "отбел", "удалить зуб", "удаление зуб",
)

_WS_RE = re.compile(r"\s+")


def load_root_env() -> None:
    """TG_API_ID/TG_API_HASH из корневого .env (не перетирая окружение)."""
    env_path = BASE.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def anonymize(body: str) -> str:
    body = PHONE_RE.sub("[phone]", body)
    body = LINK_RE.sub("[link]", body)
    return MENTION_RE.sub("[user]", body)


def normalize(body: str) -> str:
    return _WS_RE.sub(" ", body.strip().casefold())


def text_hash(body: str) -> str:
    return hashlib.sha256(normalize(body).encode("utf-8")).hexdigest()


def is_dental(body: str) -> bool:
    low = body.casefold()
    return any(stem in low for stem in DENTAL_STEMS)


def is_promo(message) -> bool:
    """Реклама/посты клиник: ссылки, проценты-скидки, эмодзи-простыни."""
    body = message.message or ""
    if LINK_RE.search(body) or PROMO_RE.search(body):
        return True
    return len(EMOJI_RE.findall(body)) > 3


# узбекские маркеры (латиница + кириллица) — отсев чисто русского/спама
# в general-режиме: нам важна именно узбекская/смешанная речь
UZ_MARKERS = (
    "ʻ", "ʼ", "oʻ", "gʻ", "o'", "g'", "bo'l", "qil", "kerak", "qancha",
    "bormi", "mumkin", "iltimos", "rahmat", "yaxshi", "necha", "qachon",
    "boʻladi", "yozil", "yoz", "narx",
    "бўл", "керак", "қанча", "борми", "мумкин", "илтимос", "рахмат", "яхши",
    "неча", "қачон", "ёзил", "нарх",
)


_CYR_RE = re.compile(r"[а-яёўқғҳ]", re.IGNORECASE)
_LAT_RE = re.compile(r"[a-z]", re.IGNORECASE)


def looks_uzbek(body: str) -> bool:
    low = body.casefold()
    return any(m in low for m in UZ_MARKERS)


def is_conversational(body: str) -> bool:
    """Разговорный текст на ru или uz (латиница/кириллица) — для general-сбора
    узбекского сленга И русского (решение пользователя: «чем больше, тем лучше»).
    Требуем достаточно букв, чтобы отсеять эмодзи/числа/обрывки."""
    letters = len(_CYR_RE.findall(body)) + len(_LAT_RE.findall(body))
    return letters >= 12


def keep(message, counts: dict, topic: str = "dental") -> str | None:
    """Текст сообщения, если оно прошло фильтры; иначе None (+счётчик).

    topic=dental — требуем стоматологическую лексику (речь пациентов клиник);
    topic=general — без неё, но требуем узбекские маркеры (сбор стиля/сленга).
    """
    body = (message.message or "").strip()
    if not body or not (MIN_LEN <= len(body) <= MAX_LEN):
        counts["len"] += 1
        return None
    if message.via_bot_id or message.fwd_from or message.post or (
            getattr(message.sender, "bot", False)):
        counts["bot_fwd"] += 1
        return None
    if is_promo(message) or CLINIC_POST_RE.search(body):
        counts["promo"] += 1
        return None
    if topic == "general":
        if not is_conversational(body):
            counts["off_topic"] += 1
            return None
    elif not is_dental(body):
        counts["off_topic"] += 1
        return None
    return anonymize(body)


def load_seen() -> set[str]:
    if SEEN_PATH.exists():
        return set(SEEN_PATH.read_text(encoding="utf-8").split())
    return set()


def append_seen(hashes: list[str]) -> None:
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SEEN_PATH.open("a", encoding="utf-8") as fh:
        for h in hashes:
            fh.write(h + "\n")


async def harvest_source(client, src: dict, cap: int, history: int,
                         posts: int, seen: set[str], counts: dict) -> list[str]:
    """Тексты одного источника, прошедшие фильтры (≤cap)."""
    topic = src.get("topic", "dental")
    entity = await client.get_entity(src["name"])
    kept: list[str] = []

    async def consume(iterator):
        async for message in iterator:
            counts["fetched"] += 1
            body = keep(message, counts, topic)
            if body is None:
                continue
            h = text_hash(body)
            if h in seen:
                counts["duplicate"] += 1
                continue
            seen.add(h)
            kept.append(body)
            if len(kept) >= cap:
                return

    if src.get("kind") == "channel_comments":
        # комментарии пациентов под постами клиники — ближе всего к booking
        async for post in client.iter_messages(entity, limit=posts):
            if len(kept) >= cap:
                break
            try:
                await consume(client.iter_messages(
                    entity, reply_to=post.id, limit=200))
            except Exception as e:  # у поста нет комментариев и т.п.
                print(f"    (пост {post.id}: {e})")
    else:
        await consume(client.iter_messages(entity, limit=history))
    return kept


async def run_harvest(args) -> int:
    api_id, api_hash = os.environ.get("TG_API_ID"), os.environ.get("TG_API_HASH")
    if not api_id or not api_hash:
        print("[FAIL] нет TG_API_ID/TG_API_HASH (my.telegram.org → .env)")
        return 1
    config = json.loads(Path(args.sources).read_text(encoding="utf-8"))
    sources = config.get("sources", [])

    client = TelegramClient(SESSION, int(api_id), api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        print("[FAIL] не залогинен — сначала: python harvest_login.py "
              "request --phone \"+998...\"  затем  code <КОД>")
        await client.disconnect()
        return 1

    if args.discover:
        result = await client(SearchRequest(q=args.discover, limit=20))
        print(f"Кандидаты по «{args.discover}» (в конфиг добавлять руками):")
        for chat in result.chats:
            username = getattr(chat, "username", None)
            members = getattr(chat, "participants_count", "?")
            print(f"  @{username or '—'}  «{chat.title}»  (~{members} уч.)")
        await client.disconnect()
        return 0

    if not sources:
        print("[FAIL] harvest_sources.json пуст — добавь источники "
              "(--discover в помощь)")
        await client.disconnect()
        return 1

    seen = load_seen()
    counts = {"fetched": 0, "len": 0, "bot_fwd": 0, "promo": 0,
              "off_topic": 0, "duplicate": 0}
    all_kept: list[tuple[str, str]] = []  # (topic, text)
    for i, src in enumerate(sources):
        topic = src.get("topic", "dental")
        print(f"[{i + 1}/{len(sources)}] {src['name']} "
              f"({src.get('kind', 'group')}/{topic})…")
        try:
            kept = await harvest_source(client, src, args.per_source_cap,
                                        args.history_limit, args.posts,
                                        seen, counts)
        except FloodWaitError as e:
            print(f"    FloodWait {e.seconds}s — жду и пропускаю источник")
            await asyncio.sleep(e.seconds + 5)
            continue
        except Exception as e:
            print(f"    [SKIP] {e}")
            continue
        print(f"    принято {len(kept)}")
        all_kept.extend((topic, body) for body in kept)
        if i + 1 < len(sources):
            await asyncio.sleep(PAUSE_BETWEEN_SOURCES)
    await client.disconnect()

    stamp = f"{date.today():%Y%m%d}"
    out = Path(args.out) if args.out else (
        BASE / "data" / "inbox" / f"harvest_{stamp}.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for n, (topic, body) in enumerate(all_kept, 1):
            fh.write(json.dumps(
                {"id": f"hv_{stamp}_{n:04d}", "text": body, "source": "harvest",
                 "category": f"harvest_{topic}", "gold": None},
                ensure_ascii=False) + "\n")
    append_seen([text_hash(b) for _, b in all_kept])

    print(f"\nвсего прочитано: {counts['fetched']}; отброшено — "
          f"длина: {counts['len']}, боты/форварды: {counts['bot_fwd']}, "
          f"реклама: {counts['promo']}, офтоп: {counts['off_topic']}, "
          f"дубли: {counts['duplicate']}")
    print(f"[OK] собрано {len(all_kept)} → {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Харвестер публичной "
                                     "стоматологической речи (Telethon)")
    parser.add_argument("--sources", default=str(BASE / "harvest_sources.json"))
    parser.add_argument("--out", default=None)
    parser.add_argument("--per-source-cap", type=int, default=200)
    parser.add_argument("--history-limit", type=int, default=1000)
    parser.add_argument("--posts", type=int, default=30,
                        help="постов канала для чтения комментариев")
    parser.add_argument("--discover", default=None,
                        help="поиск публичных источников по слову (только печать)")
    args = parser.parse_args()
    load_root_env()
    return asyncio.run(run_harvest(args))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
