"""Реальный NLU-экстрактор: gpt-4o-mini, structured outputs, один repair.

ДЕНЬГИ: каждый extract() — платный вызов OpenAI API. Подключается только
явным флагом (демо --real); тесты и разработка живут на FakeExtractor.
Деидентификация текста перед отправкой — инкремент 3 (channel adapter).
"""
from __future__ import annotations

import logging
from pathlib import Path

from pydantic import ValidationError

from navbat.nlu.extractor import ExtractionError, ProviderDownError
from navbat.nlu.schema import Extraction

log = logging.getLogger("navbat.nlu")

DEFAULT_MODEL = "gpt-4o-mini"  # проверено спайком: тянет узбекский/русский/суржик
_PROMPT_PATH = Path(__file__).parent / "prompts" / "system.md"
REPAIR_TRIES = 1


LLM_TIMEOUT = 8.0  # BRIEF: таймаут LLM 8 сек → graceful degradation


class OpenAIExtractor:
    def __init__(self, model: str = DEFAULT_MODEL, on_usage=None) -> None:
        # ленивый импорт: openai — optional-зависимость [llm]
        from openai import OpenAI

        self._client = OpenAI(timeout=LLM_TIMEOUT, max_retries=2)
        self._model = model
        self._on_usage = on_usage  # callable(in_tokens, out_tokens) — учёт бюджета
        self._system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")

    def extract(self, text: str) -> Extraction:
        from openai import APIError  # модуль уже загружен в __init__

        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": text},
        ]
        last_error: Exception | None = None
        for attempt in range(REPAIR_TRIES + 1):
            if attempt:
                # repair: повтор полезен, только если модель видит, ЧТО не прошло
                messages.append({
                    "role": "user",
                    "content": f"Твой прошлый ответ не прошёл валидацию схемы: "
                               f"{str(last_error)[:300]}. Верни исправленный JSON "
                               f"строго по допустимым значениям.",
                })
            try:
                response = self._client.chat.completions.parse(
                    model=self._model,
                    messages=messages,
                    response_format=Extraction,
                    temperature=0,
                    max_completion_tokens=2000,
                )
                if self._on_usage and response.usage:
                    self._on_usage(response.usage.prompt_tokens,
                                   response.usage.completion_tokens)
                message = response.choices[0].message
                if message.refusal or message.parsed is None:
                    raise ExtractionError(message.refusal or "пустой parsed")
                return message.parsed
            except APIError as e:
                # сеть/5xx/429 после встроенных ретраев SDK — аутэйдж
                # провайдера, топливо для FallbackExtractor
                raise ProviderDownError(f"openai: {e}") from e
            except (ValidationError, ExtractionError) as e:
                last_error = e
                log.warning("NLU: невалидный ответ (попытка %d): %s", attempt + 1, e)
        raise ExtractionError(f"NLU не дал валидный JSON после repair: {last_error}")
