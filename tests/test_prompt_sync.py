"""Синхронизация копий системного промпта NLU.

Прод-экстракторы читают src/navbat/nlu/prompts/system.md, eval-харнесс —
spike_nlu/prompts/system.md. 12.06.2026 копии разъехались (конвенция оплаты
доехала только до спайка) — дрейф ловится байтовым сравнением. Для
fine-tuning синхронность критична: тренировочный промпт обязан совпадать
с прод-инференсом.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROD_PROMPT = ROOT / "src" / "navbat" / "nlu" / "prompts" / "system.md"
SPIKE_PROMPT = ROOT / "spike_nlu" / "prompts" / "system.md"


def test_prod_prompt_equals_spike_prompt():
    assert PROD_PROMPT.read_bytes() == SPIKE_PROMPT.read_bytes(), (
        "копии системного промпта разъехались: правки вносить в ОБА файла "
        f"({PROD_PROMPT.relative_to(ROOT)} и {SPIKE_PROMPT.relative_to(ROOT)})"
    )
