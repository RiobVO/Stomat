"""Экспорт обезличенного корпуса сообщений пациентов для разметки NLU.

Зачем: data flywheel — реальные тексты из message_queue живут ≤90 дней
(retention) и без экспорта теряются; размеченный корпус — топливо eval'а
и fine-tuning (spike_nlu/). Запуск ТОЛЬКО по явной команде оператора:

    python -m navbat.export_corpus --clinic <uuid> [--out FILE]
                                   [--min-len 2] [--limit N]

Приватность (PRIVACY.md разд. 3/7/9): выбирается ТОЛЬКО текст сообщения —
Telegram-метаданные (from.id/first_name/username, chat.id) в экспорт не
попадают by construction; телефоны → [phone] (общий redact_phones),
@упоминания → [user], ссылки → [link]; админ-чаты исключены; контакты
(кнопка «Поделиться») не выбираются. Имена в свободном тексте остаются —
то же ограничение, что у LLM-границы и копилки unanswered_question.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import date
from pathlib import Path

from sqlalchemy import text

from navbat.db.base import make_app_engine, make_session_factory, tenant_transaction
from navbat.dialog.replies import TEMPLATES
from navbat.envfile import load_env_file
from navbat.nlu.wrappers import redact_phones

_WS_RE = re.compile(r"\s+")
_MENTION_RE = re.compile(r"@\w{3,}")
_LINK_RE = re.compile(r"(?:https?://|t\.me/)\S+", re.IGNORECASE)

# label'ы постоянного reply-меню приходят текстом и перехватываются до NLU —
# для корпуса это шум; оба языка
_MENU_KEYS = ("btn_menu_book", "btn_menu_resched", "btn_menu_cancel",
              "btn_menu_prices", "btn_menu_about", "btn_menu_lang")
MENU_LABELS = frozenset(
    TEMPLATES[key][lang] for key in _MENU_KEYS for lang in ("ru", "uz"))

_SELECT_TEXTS = text("""
    SELECT payload->'message'->>'text' AS body
    FROM message_queue
    WHERE status IN ('done', 'failed')
      AND payload ? 'message'
      AND payload->'message' ? 'text'
      AND NOT (payload->'message' ? 'contact')
      AND tg_chat_id NOT IN (
          SELECT unnest(tg_admin_chat_ids) FROM clinic
          WHERE id = current_setting('app.clinic_id')::uuid)
    ORDER BY id
""")


def anonymize(body: str) -> str:
    """Телефоны/упоминания/ссылки → плейсхолдеры (граница приватности)."""
    body = redact_phones(body)
    body = _LINK_RE.sub("[link]", body)
    return _MENTION_RE.sub("[user]", body)


def _normalize(body: str) -> str:
    return _WS_RE.sub(" ", body.strip().casefold())


def export_corpus(session_factory, clinic_id: uuid.UUID, *,
                  min_len: int = 2, limit: int | None = None
                  ) -> tuple[list[dict], dict[str, int]]:
    """Записи корпуса (gold=null — inbox для разметки) + счётчики отбраковки."""
    with tenant_transaction(session_factory, clinic_id) as session:
        rows = session.execute(_SELECT_TEXTS).scalars().all()
    counts = {"fetched": len(rows), "command": 0, "menu_label": 0,
              "short": 0, "duplicate": 0, "exported": 0}
    seen: set[str] = set()
    records: list[dict] = []
    for body in rows:
        stripped = body.strip()
        if stripped.startswith("/"):
            counts["command"] += 1
            continue
        if stripped in MENU_LABELS:
            counts["menu_label"] += 1
            continue
        if len(stripped) < min_len:
            counts["short"] += 1
            continue
        key = _normalize(stripped)
        if key in seen:
            counts["duplicate"] += 1
            continue
        seen.add(key)
        records.append({
            "id": f"p_{len(records) + 1:04d}",
            "text": anonymize(stripped),
            "source": "pilot",
            "category": "pilot_raw",
            "gold": None,
        })
        if limit is not None and len(records) >= limit:
            break
    counts["exported"] = len(records)
    return records, counts


def write_jsonl(records: list[dict], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main() -> int:
    load_env_file()
    parser = argparse.ArgumentParser(
        description="Экспорт обезличенного корпуса сообщений для разметки NLU")
    parser.add_argument("--clinic", required=True, type=uuid.UUID)
    parser.add_argument("--out", type=Path,
                        default=Path("spike_nlu/data/inbox") /
                        f"pilot_{date.today():%Y%m%d}.jsonl")
    parser.add_argument("--min-len", type=int, default=2)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    engine = make_app_engine()
    try:
        session_factory = make_session_factory(engine)
        records, counts = export_corpus(
            session_factory, args.clinic,
            min_len=args.min_len, limit=args.limit)
    finally:
        engine.dispose()
    write_jsonl(records, args.out)
    print(f"[OK] всего строк очереди: {counts['fetched']}; отброшено — "
          f"команд: {counts['command']}, кнопок меню: {counts['menu_label']}, "
          f"коротких: {counts['short']}, дублей: {counts['duplicate']}")
    print(f"[OK] экспортировано {counts['exported']} → {args.out}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
