"""Одноразовый перенос голдов v1+real на конвенции от 2026-06-05.

Enum интентов сужен до 5 (book|reschedule|cancel|question|other), kids убран,
симптом без услуги -> checkup, срочность -> today, is_medical как отдельный флаг.
v2 (synthetic_gemini/gpt) не трогаем — размечен по чужой конвенции.
"""
import json
import shutil

SRC = "data/messages.jsonl"

INTENT = {
    # просьба мед-совета / «это нормально?» -> question (+is_medical)
    "m008": "question", "m023": "question", "m029": "question",
    "m052": "question", "m060": "question", "m130": "question",
    # «выпало/сломалось, что делать» -> book (лид)
    "m080": "book", "m095": "book",
    # greeting/complaint вне enum -> other
    "m019": "other", "m040": "other", "m058": "other",
    "m061": "other", "m065": "other", "m090": "other",
    # real: голые симптомы -> book
    "r_005": "book", "r_007": "book", "r_013": "book", "r_017": "book",
    "u_005": "book", "u_007": "book", "u_011": "book", "u_013": "book", "u_017": "book",
}
SERVICE = {
    # kids убран: «для ребёнка» не услуга
    "m009": None, "m041": None, "m056": None, "m074": None, "m118": None,
    # «показать зуб» = осмотр
    "m102": "checkup", "m128": "checkup",
    # симптом без услуги -> checkup
    "m071": "checkup", "m121": "checkup", "m123": "checkup",
    "r_001": "checkup", "r_007": "checkup", "r_011": "checkup",
    "r_013": "checkup", "r_017": "checkup",
    "u_001": "checkup", "u_007": "checkup", "u_011": "checkup",
    "u_013": "checkup", "u_017": "checkup",
}
DATE = {"r_020": "today", "u_020": "today"}  # срочность -> today
MEDICAL_TRUE = {
    "m008", "m023", "m029", "m052", "m060", "m071", "m080", "m095",
    "m121", "m123", "m129", "m130",
    "r_001", "r_005", "r_007", "r_011", "r_013", "r_017", "r_020",
    "u_001", "u_005", "u_007", "u_011", "u_013", "u_017", "u_020",
}
NOTES = {
    "r_011": "kids убран по конвенции: ребёнок не услуга; checkup по симптому",
    "u_011": "kids не услуга; голый симптом -> book+checkup по конвенции",
}

shutil.copy(SRC, "data/messages_backup_pre_conventions.jsonl")
out, changed = [], 0
for line in open(SRC, encoding="utf-8").read().splitlines():
    if not line.strip():
        continue
    rec = json.loads(line)
    rid, gold = rec["id"], rec["gold"]
    before = json.dumps(gold, ensure_ascii=False, sort_keys=True)
    if rid in INTENT:
        gold["intent"] = INTENT[rid]
    if rid in SERVICE:
        gold["service"] = SERVICE[rid]
    if rid in DATE:
        gold["date_ref"] = DATE[rid]
    if rid in NOTES:
        rec["note"] = NOTES[rid]
    # is_medical размечаем только там, где конвенцию контролируем (v1 + real)
    if rec["source"] in ("synthetic", "real"):
        gold["is_medical"] = rid in MEDICAL_TRUE
    if json.dumps(gold, ensure_ascii=False, sort_keys=True) != before:
        changed += 1
    out.append(rec)

with open(SRC, "w", encoding="utf-8") as fh:
    for rec in out:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
print(f"записей: {len(out)}, изменено голдов: {changed}")
