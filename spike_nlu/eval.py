"""Ф0-спайк: NLU-eval-харнесс для гипотезы №1 (см. BRIEF.md, разд. 5 и 13).

Прогоняет data/messages.jsonl через дешёвые модели OpenAI, сравнивает
извлечённые слоты с gold-разметкой, считает точность по полям, токены
и стоимость. Одноразовый код, на выброс.

Запуск:
    python eval.py --dry-run                       # валидация датасета без API
    python eval.py --limit 5 --models gpt-5-nano   # smoke
    python eval.py                                 # полный прогон 3 моделей
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError, field_validator

log = logging.getLogger("nlu_eval")

# ── Константы ────────────────────────────────────────────────────────────────

# USD за 1M токенов (input, output). Сверено 2026-06-05:
# https://developers.openai.com/api/docs/pricing
PRICES_USD_PER_1M = {
    "gpt-5-nano": (0.05, 0.40),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4o-mini": (0.15, 0.60),
}
UZS_PER_USD = 12_500   # ориентир на июнь 2026; проверить курс перед выводами по cost model
MSGS_PER_DAY = 30      # для экстраполяции; реальный трафик — открытый вопрос брифа (п. 15)
STRICT_THRESHOLD = 0.85  # kill-критерий гипотезы №1
API_MAX_TRIES = 4      # 429/5xx: повторы с backoff
REPAIR_TRIES = 1       # невалидный JSON: один повтор (счётчик repair идёт в отчёт)

DEFAULT_MODELS = "gpt-5-nano,gpt-4.1-nano,gpt-4o-mini"
FIELDS = ("intent", "service", "doctor", "date_ref", "time_ref", "language")

DATE_REF_RE = re.compile(
    r"^(today|tomorrow|after_tomorrow|next_week"
    r"|weekday_(mon|tue|wed|thu|fri|sat|sun)|explicit_\d{2}\.\d{2})$"
)
TIME_REF_RE = re.compile(r"^(([01]\d|2[0-3]):[0-5]\d|morning|afternoon|evening)$")


# ── Схема извлечения (она же валидирует gold-разметку) ──────────────────────

class Extraction(BaseModel):
    # конвенции 2026-06-05: 5 интентов (medical/greeting/complaint -> книга/вопрос/other),
    # kids убран, медицинский сигнал -> отдельный флаг is_medical
    intent: Literal["book", "reschedule", "cancel", "question", "other"]
    service: Optional[Literal[
        "cleaning", "filling", "extraction", "implant", "crown",
        "whitening", "braces", "checkup", "xray",
    ]]
    doctor: Optional[str]
    # date_ref/time_ref — открытые строки (union в Literal structured outputs не умеет),
    # поэтому формат добиваем regex-валидаторами: кривое значение = ошибка парсинга → repair.
    date_ref: Optional[str]
    time_ref: Optional[str]
    language: Literal["uz", "ru", "mixed"]
    is_medical: bool  # симптом/боль/просьба мед-совета — флаг для дисклеймера/эскалации, не интент

    @field_validator("date_ref")
    @classmethod
    def check_date_ref(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not DATE_REF_RE.match(v):
            raise ValueError(f"date_ref вне формата: {v!r}")
        return v

    @field_validator("time_ref")
    @classmethod
    def check_time_ref(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not TIME_REF_RE.match(v):
            raise ValueError(f"time_ref вне формата: {v!r}")
        return v


class GoldExtraction(Extraction):
    """Gold-разметка: is_medical может отсутствовать (сет v2 не размечен)."""
    is_medical: Optional[bool] = None


class RefusalError(Exception):
    """Модель отказалась отвечать или вернула пустой parsed."""


# ── Загрузка датасета ────────────────────────────────────────────────────────

def load_dataset(path: Path, limit: Optional[int]) -> list[dict]:
    records, errors = [], []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            for key in ("id", "text", "source", "category", "gold"):
                if key not in rec:
                    raise ValueError(f"нет поля {key!r}")
            rec["gold"] = GoldExtraction.model_validate(rec["gold"])
            records.append(rec)
        except (json.JSONDecodeError, ValidationError, ValueError) as e:
            errors.append(f"строка {lineno}: {e}")
    if errors:
        for err in errors:
            log.error("кривая gold-разметка: %s", err)
        raise SystemExit(f"[FAIL] датасет не прошёл валидацию: {len(errors)} ошибок")
    return records[:limit] if limit else records


# ── Вызов модели ─────────────────────────────────────────────────────────────

def _model_kwargs(model: str, reasoning_effort: str) -> dict:
    # gpt-5-* — reasoning-модели: temperature не принимают, effort влияет на цену
    if model.startswith("gpt-5"):
        return {"reasoning_effort": reasoning_effort}
    return {"temperature": 0}


async def call_model(
    client: AsyncOpenAI, model: str, system_prompt: str, text: str,
    reasoning_effort: str, use_fallback: bool, repair_hint: Optional[str] = None,
) -> tuple[Extraction, object]:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": text},
    ]
    if repair_hint:
        # repair: говорим модели, ЧТО именно не прошло валидацию, иначе повтор бесполезен
        messages.append({
            "role": "user",
            "content": f"Твой прошлый ответ не прошёл валидацию схемы: {repair_hint}. "
                       f"Верни исправленный JSON строго по допустимым значениям.",
        })
    kwargs = dict(
        model=model,
        messages=messages,
        max_completion_tokens=2000,
        **_model_kwargs(model, reasoning_effort),
    )
    if not use_fallback:
        resp = await client.chat.completions.parse(response_format=Extraction, **kwargs)
        msg = resp.choices[0].message
        if msg.refusal:
            raise RefusalError(msg.refusal)
        if msg.parsed is None:
            raise RefusalError("parsed is None (обрезан по токенам?)")
        return msg.parsed, resp.usage
    # fallback: модель не поддержала structured outputs → json_object + ручной парсинг
    resp = await client.chat.completions.create(
        response_format={"type": "json_object"}, **kwargs
    )
    content = resp.choices[0].message.content or ""
    return Extraction.model_validate_json(content), resp.usage


async def extract_one(
    client: AsyncOpenAI, model: str, system_prompt: str, rec: dict,
    sem: asyncio.Semaphore, reasoning_effort: str, fallback_flag: dict,
) -> dict:
    """Один вызов с retry: backoff на 429/5xx, один repair на кривой JSON."""
    result = {
        "id": rec["id"], "text": rec["text"], "category": rec["category"],
        "source": rec["source"], "gold": rec["gold"].model_dump(),
        "got": None, "error": None, "repairs": 0,
        "latency_ms": None, "in_tokens": 0, "out_tokens": 0, "reasoning_tokens": 0,
    }
    async with sem:
        backoffs = 0
        repair_hint = None
        while True:
            t0 = time.perf_counter()
            try:
                parsed, usage = await call_model(
                    client, model, system_prompt, rec["text"],
                    reasoning_effort, fallback_flag["on"], repair_hint,
                )
                result["got"] = parsed.model_dump()
                result["latency_ms"] = round((time.perf_counter() - t0) * 1000)
                result["in_tokens"] = usage.prompt_tokens
                result["out_tokens"] = usage.completion_tokens
                details = getattr(usage, "completion_tokens_details", None)
                result["reasoning_tokens"] = getattr(details, "reasoning_tokens", 0) or 0
                return result
            except (openai.RateLimitError, openai.APIConnectionError,
                    openai.InternalServerError) as e:
                backoffs += 1
                if backoffs >= API_MAX_TRIES:
                    log.error("%s %s: API не ответил после %d попыток: %s",
                              model, rec["id"], backoffs, e)
                    result["error"] = f"api: {e}"
                    return result
                await asyncio.sleep(2 ** backoffs)
            except openai.BadRequestError as e:
                # structured outputs не поддержан этой моделью → один раз
                # переключаем весь прогон модели на json_object
                if not fallback_flag["on"] and "response_format" in str(e):
                    log.warning("%s: structured outputs отклонён, fallback на json_object", model)
                    fallback_flag["on"] = True
                    continue
                log.error("%s %s: BadRequest: %s", model, rec["id"], e)
                result["error"] = f"bad_request: {e}"
                return result
            except (ValidationError, RefusalError, json.JSONDecodeError) as e:
                result["repairs"] += 1
                if result["repairs"] > REPAIR_TRIES:
                    log.error("%s %s: невалидный JSON после repair: %s", model, rec["id"], e)
                    result["error"] = f"invalid_json: {e}"
                    return result
                repair_hint = str(e)[:300]


async def run_model(
    model: str, records: list[dict], system_prompt: str,
    concurrency: int, reasoning_effort: str,
) -> list[dict]:
    client = AsyncOpenAI()
    sem = asyncio.Semaphore(concurrency)
    fallback_flag = {"on": False}  # общий для всех сообщений модели
    log.info("прогон %s: %d сообщений, concurrency=%d", model, len(records), concurrency)
    tasks = [
        extract_one(client, model, system_prompt, rec, sem, reasoning_effort, fallback_flag)
        for rec in records
    ]
    results = await asyncio.gather(*tasks)
    await client.close()
    return list(results)


# ── Скоринг ──────────────────────────────────────────────────────────────────

def score_fields(gold: dict, got: Optional[dict]) -> dict[str, bool]:
    """Поля сравниваются строго; doctor — substring case-insensitive
    (gold хранит каноничное упоминание, модель может вернуть словоформу)."""
    if got is None:
        return {f: False for f in FIELDS}
    scores = {}
    for f in FIELDS:
        if f == "doctor":
            g, p = gold["doctor"], got["doctor"]
            if g is None or p is None:
                scores[f] = g is None and p is None
            else:
                gl, pl = g.lower(), p.lower()
                scores[f] = gl in pl or pl in gl
        else:
            scores[f] = gold[f] == got[f]
    return scores


# ── Отчёт ────────────────────────────────────────────────────────────────────

def _pct(part: int, total: int) -> str:
    return f"{100 * part / total:.1f}%" if total else "n/a"


def _accuracy_block(results: list[dict], key: str) -> list[str]:
    """Strict accuracy в разрезе category или source."""
    groups: dict[str, list[bool]] = defaultdict(list)
    for r in results:
        groups[r[key]].append(r["strict"])
    lines = [f"| {key} | n | strict |", "|---|---|---|"]
    for name in sorted(groups):
        vals = groups[name]
        lines.append(f"| {name} | {len(vals)} | {_pct(sum(vals), len(vals))} |")
    return lines


def build_model_report(model: str, results: list[dict]) -> tuple[list[str], dict]:
    n = len(results)
    for r in results:
        r["fields_ok"] = score_fields(r["gold"], r["got"])
        r["strict"] = all(r["fields_ok"].values())

    errors = [r for r in results if r["error"]]
    repairs = sum(r["repairs"] for r in results)
    strict_n = sum(r["strict"] for r in results)
    strict_acc = strict_n / n if n else 0.0

    in_tok = sum(r["in_tokens"] for r in results)
    out_tok = sum(r["out_tokens"] for r in results)
    reasoning_tok = sum(r["reasoning_tokens"] for r in results)
    latencies = sorted(r["latency_ms"] for r in results if r["latency_ms"] is not None)

    prices = PRICES_USD_PER_1M.get(model)
    if prices:
        cost_usd = in_tok / 1e6 * prices[0] + out_tok / 1e6 * prices[1]
    else:
        log.warning("нет цены для модели %s — стоимость не считается", model)
        cost_usd = None

    lines = [f"## {model}", ""]
    lines.append(f"Сообщений: {n}, ошибок прогона: {len(errors)}, JSON-repair: {repairs}")
    lines.append("")

    # per-field
    lines += ["| поле | accuracy |", "|---|---|"]
    for f in FIELDS:
        ok = sum(r["fields_ok"][f] for r in results)
        lines.append(f"| {f} | {_pct(ok, n)} |")
    lines.append(f"| **strict (все 6)** | **{_pct(strict_n, n)}** |")
    lines.append("")

    lines += _accuracy_block(results, "category") + [""]
    lines += _accuracy_block(results, "source") + [""]

    # is_medical — отдельный флаг, не входит в strict; считается только по размеченным голдам
    labeled = [r for r in results if r["gold"].get("is_medical") is not None]
    if labeled:
        med_ok = sum(
            1 for r in labeled
            if r["got"] and r["got"]["is_medical"] == r["gold"]["is_medical"]
        )
        lines.append(f"is_medical (размечено {len(labeled)}): {_pct(med_ok, len(labeled))}")
        lines.append("")

    # intent confusion — только ошибки
    confusion = Counter(
        (r["gold"]["intent"], r["got"]["intent"])
        for r in results if r["got"] and not r["fields_ok"]["intent"]
    )
    if confusion:
        lines.append("Intent-ошибки (gold → got):")
        for (g, p), cnt in confusion.most_common():
            lines.append(f"- {g} → {p}: {cnt}")
        lines.append("")

    # фейлы целиком
    fails = [r for r in results if not r["strict"]]
    if fails:
        lines.append(f"<details><summary>Фейлы ({len(fails)})</summary>")
        lines.append("")
        for r in fails:
            if r["error"]:
                lines.append(f"- `{r['id']}` «{r['text']}» — ОШИБКА: {r['error']}")
                continue
            diffs = ", ".join(
                f"{f}: {r['gold'][f]!r}→{r['got'][f]!r}"
                for f in FIELDS if not r["fields_ok"][f]
            )
            lines.append(f"- `{r['id']}` «{r['text']}» — {diffs}")
        lines += ["", "</details>", ""]

    # экономика
    lines.append(f"Токены: in {in_tok}, out {out_tok} (из них reasoning {reasoning_tok}), "
                 f"avg/msg: {in_tok // n if n else 0} in / {out_tok // n if n else 0} out")
    if cost_usd is not None and n:
        per_msg = cost_usd / n
        monthly_usd = per_msg * MSGS_PER_DAY * 30
        lines.append(
            f"Стоимость прогона: ${cost_usd:.4f} (${per_msg * 1000:.3f}/1k сообщений). "
            f"Экстраполяция при {MSGS_PER_DAY} сообщ/день: ~${monthly_usd:.2f}/мес "
            f"≈ {monthly_usd * UZS_PER_USD:,.0f} сум/мес "
            f"(нижняя граница: без истории диалога; бриф закладывает 30–100 тыс сум)."
        )
    if latencies:
        p50 = statistics.median(latencies)
        p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
        lines.append(f"Latency: p50 {p50:.0f} ms, p95 {p95:.0f} ms")
    lines.append("")

    verdict_ok = strict_acc >= STRICT_THRESHOLD
    lines.append(
        f"{'[OK]' if verdict_ok else '[FAIL]'} strict accuracy "
        f"{strict_acc:.1%} {'≥' if verdict_ok else '<'} {STRICT_THRESHOLD:.0%}"
    )
    lines.append("")

    summary = {
        "model": model, "n": n, "errors": len(errors), "strict_acc": strict_acc,
        "fields": {f: sum(r["fields_ok"][f] for r in results) / n if n else 0 for f in FIELDS},
        "cost_usd": cost_usd, "verdict_ok": verdict_ok,
    }
    return lines, summary


def build_report(all_results: dict[str, list[dict]], records: list[dict]) -> tuple[str, list[dict]]:
    lines = [f"# NLU eval — {datetime.now():%Y-%m-%d %H:%M}", ""]

    has_real = any(r["source"] == "real" for r in records)
    if not has_real:
        lines += [
            "> **ВНИМАНИЕ: в датасете только синтетика (source=synthetic).**",
            "> Это валидация харнесса, НЕ вердикт по гипотезе №1 — бриф требует",
            "> реальный тест-сет. Добавь реальные сообщения с source=\"real\".",
            "",
        ]

    summaries = []
    sections = []
    for model, results in all_results.items():
        section, summary = build_model_report(model, results)
        sections += section
        summaries.append(summary)

    # сводная таблица сверху
    lines += ["| модель | strict | intent | service | date_ref | time_ref | $/прогон |",
              "|---|---|---|---|---|---|---|"]
    for s in summaries:
        cost = f"${s['cost_usd']:.4f}" if s["cost_usd"] is not None else "?"
        lines.append(
            f"| {s['model']} | **{s['strict_acc']:.1%}** | {s['fields']['intent']:.1%} "
            f"| {s['fields']['service']:.1%} | {s['fields']['date_ref']:.1%} "
            f"| {s['fields']['time_ref']:.1%} | {cost} |"
        )
    lines.append("")
    lines += sections
    return "\n".join(lines), summaries


# ── main ─────────────────────────────────────────────────────────────────────

def dataset_stats(records: list[dict]) -> str:
    by_cat = Counter(r["category"] for r in records)
    by_src = Counter(r["source"] for r in records)
    by_intent = Counter(r["gold"].intent for r in records)
    return (
        f"сообщений: {len(records)}\n"
        f"по категориям: {dict(by_cat)}\n"
        f"по источникам: {dict(by_src)}\n"
        f"по интентам: {dict(by_intent)}"
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description="NLU-eval-харнесс (Ф0 спайк)")
    parser.add_argument("--models", default=DEFAULT_MODELS)
    parser.add_argument("--data", default="data/messages.jsonl")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--reasoning-effort", default="minimal",
                        choices=["minimal", "low", "medium", "high"])
    parser.add_argument("--dry-run", action="store_true",
                        help="только валидация датасета и промпта, без API")
    args = parser.parse_args()

    base = Path(__file__).parent
    records = load_dataset(base / args.data, args.limit)
    system_prompt = (base / "prompts" / "system.md").read_text(encoding="utf-8")

    if args.dry_run:
        print(dataset_stats(records))
        print(f"системный промпт: {len(system_prompt)} символов")
        print(f"[OK] датасет валиден: {len(records)} gold-записей прошли схему")
        return 0

    if not os.environ.get("OPENAI_API_KEY"):
        print("[FAIL] нет OPENAI_API_KEY в окружении")
        return 1

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    all_results: dict[str, list[dict]] = {}
    for model in models:  # модели последовательно, сообщения внутри — параллельно
        all_results[model] = await run_model(
            model, records, system_prompt, args.concurrency, args.reasoning_effort
        )

    report, summaries = build_report(all_results, records)

    results_dir = base / "results"
    results_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = results_dir / f"report_{ts}.md"
    report_path.write_text(report, encoding="utf-8")
    raw_path = results_dir / f"raw_{ts}.jsonl"
    with raw_path.open("w", encoding="utf-8") as fh:
        for model, results in all_results.items():
            for r in results:
                fh.write(json.dumps({"model": model, **r}, ensure_ascii=False) + "\n")

    print(report)
    print(f"отчёт: {report_path}\nсырые ответы: {raw_path}")

    # ошибки прогона (не вердикт гипотезы): >5% несыгранных вызовов = прогон не удался
    run_ok = all(s["errors"] / s["n"] <= 0.05 for s in summaries if s["n"])
    total_calls = sum(s["n"] for s in summaries)
    total_errors = sum(s["errors"] for s in summaries)
    print(f"{'[OK]' if run_ok else '[FAIL]'} прогон завершён: "
          f"{total_calls - total_errors}/{total_calls} ответов получено")
    return 0 if run_ok else 1


if __name__ == "__main__":
    # консоль Windows по умолчанию cp1251 — кириллица в отчёте бьётся
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # по строке на каждый вызов — шум
    sys.exit(asyncio.run(main()))
