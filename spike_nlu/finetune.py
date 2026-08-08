"""Fine-tuning gpt-4o-mini под NLU Navbat: подготовка, запуск, гейт приёмки.

ДЕНЬГИ: `start` сабмитит платную тренировку (~$3/1M trained tokens) — только
по явной команде пользователя, с гвардом --max-usd и флагом-подтверждением.
Всё остальное (prepare/estimate/compare) — бесплатно и локально.

Пайплайн:
    python finetune.py prepare    # ft_train/ft_val + анти-leakage + манифест
    python finetune.py estimate   # прикидка trained tokens и $
    python finetune.py start --max-usd 9 --yes-spend-money
    python finetune.py status --job ftjob-...
    python finetune.py smoke --model ft:gpt-4o-mini-...   # 1 вызов, ~$0.001
    python finetune.py compare --base results/raw_A.jsonl --ft results/raw_B.jsonl

Критично:
- тренировочный пример = ЗЕРКАЛО прод-инференса: полный системный промпт +
  сообщение пациента -> assistant-JSON (7 полей в порядке схемы);
- eval-сеты (real_40, holdout_v1) НИКОГДА не попадают в train — пересечение
  нормализованных текстов валит prepare;
- разделение фиксируется манифестом results/ft_manifest_v1.json (sha промпта
  и данных) — он коммитится.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).parent
PROMPT_SPIKE = BASE / "prompts" / "system.md"
PROMPT_PROD = BASE.parent / "src" / "navbat" / "nlu" / "prompts" / "system.md"
TRAIN_OUT = BASE / "data" / "ft_train_v1.jsonl"
VAL_OUT = BASE / "data" / "ft_val_v1.jsonl"
MANIFEST = BASE / "results" / "ft_manifest_v1.json"

TRAIN_SOURCES = ("data/harvested_v1.jsonl", "data/messages.jsonl")
EVAL_SOURCES = ("data/holdout_v1.jsonl", "data/real_40.jsonl")

BASE_MODEL = "gpt-4o-mini-2024-07-18"  # ft требует снапшот-id
SUFFIX = "navbat-nlu-v1"
N_EPOCHS = 2          # auto на ~700 примерах выберет 3 и пробьёт бюджет
TRAIN_USD_PER_1M = 3.00   # тариф тренировки gpt-4o-mini; сверить при запуске
TOKENS_PER_EXAMPLE = 2090  # факт прогонов 12.06: ~2051 in + ~37 assistant
VAL_SHARE = 0.10

GOLD_KEYS = ("intent", "service", "doctor", "date_ref", "time_ref",
             "language", "is_medical")
CORE_FIELDS = ("intent", "service", "doctor", "date_ref", "time_ref")
_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _WS_RE.sub(" ", text.strip().casefold())


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _gold_json(gold: dict) -> str:
    """assistant-контент: 7 полей строго в порядке схемы Extraction."""
    return json.dumps({k: gold[k] for k in GOLD_KEYS}, ensure_ascii=False)


# ── prepare ──────────────────────────────────────────────────────────────────

def cmd_prepare(_args) -> int:
    if PROMPT_SPIKE.read_bytes() != PROMPT_PROD.read_bytes():
        print("[FAIL] копии system.md разъехались — прогони tests/"
              "test_prompt_sync.py и синхронизируй до подготовки")
        return 1
    system_prompt = PROMPT_SPIKE.read_text(encoding="utf-8")

    train_records: list[dict] = []
    for rel in TRAIN_SOURCES:
        path = BASE / rel
        if not path.exists():
            print(f"[WARN] {rel} нет — пропускаю (harvested_v1 появится "
                  f"после И-4)")
            continue
        for rec in load_jsonl(path):
            if rec.get("source") == "real":
                continue  # real_40 живёт внутри messages.jsonl — это eval-сет
            if rec.get("gold") is None:
                print(f"[FAIL] {rel}: запись {rec.get('id')} без gold — "
                      f"в train только размеченное")
                return 1
            train_records.append(rec)

    missing_med = [r["id"] for r in train_records
                   if r["gold"].get("is_medical") is None]
    if missing_med:
        print(f"[FAIL] is_medical не размечен у {len(missing_med)} записей "
              f"(первые: {missing_med[:5]}) — бэкфилл И-4 обязателен")
        return 1

    eval_norms: set[str] = set()
    for rel in EVAL_SOURCES:
        eval_norms |= {normalize(r["text"]) for r in load_jsonl(BASE / rel)}
    leaks = [r["id"] for r in train_records
             if normalize(r["text"]) in eval_norms]
    if leaks:
        print(f"[FAIL] утечка eval-сета в train: {leaks} — убрать из train")
        return 1

    # train/val: 10% стратифицированно по intent (каждый 10-й внутри интента)
    by_intent: dict[str, list[dict]] = defaultdict(list)
    for rec in train_records:
        by_intent[rec["gold"]["intent"]].append(rec)
    train, val = [], []
    for intent in sorted(by_intent):
        for i, rec in enumerate(by_intent[intent]):
            (val if i % int(1 / VAL_SHARE) == 0 else train).append(rec)

    for out, records in ((TRAIN_OUT, train), (VAL_OUT, val)):
        with out.open("w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps({"messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": rec["text"]},
                    {"role": "assistant", "content": _gold_json(rec["gold"])},
                ]}, ensure_ascii=False) + "\n")

    MANIFEST.parent.mkdir(exist_ok=True)
    MANIFEST.write_text(json.dumps({
        "base_model": BASE_MODEL, "suffix": SUFFIX, "n_epochs": N_EPOCHS,
        "system_prompt_sha256": sha256_file(PROMPT_SPIKE),
        "train_sources": {rel: sha256_file(BASE / rel)
                          for rel in TRAIN_SOURCES if (BASE / rel).exists()},
        "eval_sources": {rel: sha256_file(BASE / rel)
                         for rel in EVAL_SOURCES},
        "train_examples": len(train), "val_examples": len(val),
        "real_filtered_out": True,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[OK] train: {len(train)}, val: {len(val)} → {TRAIN_OUT.name}/"
          f"{VAL_OUT.name}; манифест: {MANIFEST}")
    return 0


# ── estimate / start / status ────────────────────────────────────────────────

def _estimate_usd() -> tuple[int, float]:
    n_train = sum(1 for _ in TRAIN_OUT.open(encoding="utf-8"))
    trained_tokens = n_train * TOKENS_PER_EXAMPLE * N_EPOCHS
    return n_train, trained_tokens / 1e6 * TRAIN_USD_PER_1M


def cmd_estimate(_args) -> int:
    if not TRAIN_OUT.exists():
        print("[FAIL] сначала prepare")
        return 1
    n, usd = _estimate_usd()
    print(f"train {n} примеров × ~{TOKENS_PER_EXAMPLE} ток. × {N_EPOCHS} эпохи "
          f"≈ {n * TOKENS_PER_EXAMPLE * N_EPOCHS / 1e6:.2f}M trained tokens "
          f"≈ ${usd:.2f} (тариф ${TRAIN_USD_PER_1M}/1M — сверить)")
    return 0


def cmd_start(args) -> int:
    from openai import OpenAI  # деньги — импорт по месту

    if not args.yes_spend_money:
        print("[FAIL] тренировка платная: добавь --yes-spend-money "
              "(и подтверди запуск у владельца бюджета)")
        return 1
    if not TRAIN_OUT.exists():
        print("[FAIL] сначала prepare")
        return 1
    n, usd = _estimate_usd()
    if usd > args.max_usd:
        print(f"[FAIL] оценка ${usd:.2f} превышает гвард --max-usd "
              f"{args.max_usd} — уменьшить train или поднять лимит осознанно")
        return 1
    client = OpenAI()
    train_file = client.files.create(file=TRAIN_OUT.open("rb"),
                                     purpose="fine-tune")
    val_file = client.files.create(file=VAL_OUT.open("rb"),
                                   purpose="fine-tune")
    job = client.fine_tuning.jobs.create(
        model=BASE_MODEL,
        training_file=train_file.id,
        validation_file=val_file.id,
        suffix=SUFFIX,
        hyperparameters={"n_epochs": N_EPOCHS},
    )
    print(f"[OK] job: {job.id} (оценка ${usd:.2f}, {n} примеров). "
          f"Дальше: python finetune.py status --job {job.id}")
    return 0


def cmd_status(args) -> int:
    from openai import OpenAI

    client = OpenAI()
    job = client.fine_tuning.jobs.retrieve(args.job)
    print(f"status: {job.status}")
    if job.trained_tokens:
        print(f"trained_tokens: {job.trained_tokens} "
              f"(факт ≈ ${job.trained_tokens / 1e6 * TRAIN_USD_PER_1M:.2f})")
    if job.fine_tuned_model:
        print(f"model: {job.fine_tuned_model}")
        print(f"дальше: python finetune.py smoke --model {job.fine_tuned_model}")
    elif job.status == "failed":
        print(f"error: {job.error}")
    return 0


# ── smoke / compare ──────────────────────────────────────────────────────────

def cmd_smoke(args) -> int:
    """1 вызов ft-модели через structured outputs — совместимость до eval."""
    from openai import OpenAI

    from eval import Extraction  # зеркало прод-схемы в харнессе

    client = OpenAI()
    response = client.chat.completions.parse(
        model=args.model,
        messages=[
            {"role": "system",
             "content": PROMPT_SPIKE.read_text(encoding="utf-8")},
            {"role": "user", "content": "Ertaga tish tozalashga yozilsam "
                                        "bo'ladimi?"},
        ],
        response_format=Extraction,
        temperature=0,
    )
    parsed = response.choices[0].message.parsed
    print(f"[OK] structured outputs работает: {parsed}")
    return 0


def _core_ok(row: dict) -> bool:
    fields_ok = row.get("fields_ok") or {}
    return all(fields_ok.get(f) for f in CORE_FIELDS)


def _field_acc(rows: list[dict], field: str) -> float:
    scored = [r for r in rows if r.get("fields_ok")]
    return (sum(1 for r in scored if r["fields_ok"][field]) / len(scored)
            if scored else 0.0)


def cmd_compare(args) -> int:
    """Парный гейт приёмки по сырым ответам eval (один и тот же датасет)."""
    base = {r["id"]: r for r in load_jsonl(Path(args.base))}
    ft = {r["id"]: r for r in load_jsonl(Path(args.ft))}
    common = sorted(set(base) & set(ft))
    if not common:
        print("[FAIL] нет общих id — raw-файлы от разных датасетов?")
        return 1
    fixes = sum(1 for i in common if not _core_ok(base[i]) and _core_ok(ft[i]))
    breaks = sum(1 for i in common if _core_ok(base[i]) and not _core_ok(ft[i]))
    net = fixes - breaks
    print(f"n={len(common)}: fixes={fixes}, breaks={breaks}, нетто {net:+d}")
    for field in ("intent", "service"):
        delta = (_field_acc(list(ft.values()), field)
                 - _field_acc(list(base.values()), field)) * 100
        print(f"  {field}: {delta:+.1f} п.п.")
    for i in common:
        if _core_ok(base[i]) != _core_ok(ft[i]):
            mark = "FIX " if _core_ok(ft[i]) else "BREAK"
            print(f"  [{mark}] {i} «{ft[i]['text'][:60]}»")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Fine-tuning NLU Navbat")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prepare")
    sub.add_parser("estimate")
    p_start = sub.add_parser("start")
    p_start.add_argument("--max-usd", type=float, default=9.0)
    p_start.add_argument("--yes-spend-money", action="store_true")
    p_status = sub.add_parser("status")
    p_status.add_argument("--job", required=True)
    p_smoke = sub.add_parser("smoke")
    p_smoke.add_argument("--model", required=True)
    p_cmp = sub.add_parser("compare")
    p_cmp.add_argument("--base", required=True)
    p_cmp.add_argument("--ft", required=True)
    args = parser.parse_args()
    return {"prepare": cmd_prepare, "estimate": cmd_estimate,
            "start": cmd_start, "status": cmd_status,
            "smoke": cmd_smoke, "compare": cmd_compare}[args.cmd](args)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
