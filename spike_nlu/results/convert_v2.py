"""Одноразовый конвертер второго сета (gemini/chatgpt) в схему харнесса.

Старые записи (с gold) проходят без изменений. Новые (с expected) нормализуются:
service_canonical→service, маппинг словарей date_ref/service, новые id, source по блоку.
"""
import json
import re
import shutil
import sys

SRC = "data/messages.jsonl"

WEEKDAYS = {
    "monday": "weekday_mon", "tuesday": "weekday_tue", "wednesday": "weekday_wed",
    "thursday": "weekday_thu", "friday": "weekday_fri", "saturday": "weekday_sat",
    "sunday": "weekday_sun",
}
SERVICE_MAP = {"consultation": "checkup"}  # остальные ключи v2 совпадают с нашими


def conv_date_ref(v):
    if v is None or v in ("today", "tomorrow"):
        return v
    if v == "day_after":
        return "after_tomorrow"
    if v.startswith("weekday:"):
        return WEEKDAYS[v.split(":", 1)[1]]
    m = re.fullmatch(r"date:\d{4}-(\d{2})-(\d{2})", v)
    if m:
        return f"explicit_{m.group(2)}.{m.group(1)}"
    raise ValueError(f"неизвестный date_ref: {v!r}")


def convert(rec, block, idx):
    e = rec["expected"]
    return {
        "id": f"{'g' if block == 'gemini' else 'c'}_{idx:03d}",
        "text": rec["text"],
        "source": f"synthetic_{'gemini' if block == 'gemini' else 'gpt'}",
        "category": "v2",  # стилевых категорий во втором сете нет
        "gold": {
            "intent": e["intent"],
            "service": SERVICE_MAP.get(e["service_canonical"], e["service_canonical"]),
            "doctor": e["doctor"],
            "date_ref": conv_date_ref(e["date_ref"]),
            "time_ref": e["time_ref"],
            "language": e["language"],
        },
    }


def main():
    shutil.copy(SRC, "data/messages_backup_raw_v2.jsonl")
    out, block, idx = [], None, 0
    for line in open(SRC, encoding="utf-8").read().splitlines():
        s = line.strip()
        if not s:
            continue
        if s in ("gemini", "chatgpt"):  # заголовки блоков из ручной вставки
            block, idx = s, 0
            continue
        rec = json.loads(s)
        if "gold" in rec:
            out.append(rec)  # первый сет — без изменений
            continue
        if block is None:
            raise SystemExit(f"запись с expected до заголовка блока: {rec['id']}")
        idx += 1
        out.append(convert(rec, block, idx))

    with open(SRC, "w", encoding="utf-8") as fh:
        for rec in out:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"записано {len(out)} записей (бэкап: data/messages_backup_raw_v2.jsonl)")


if __name__ == "__main__":
    sys.exit(main())
