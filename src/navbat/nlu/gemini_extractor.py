"""Запасной NLU-экстрактор: Gemini generateContent тонким httpx-клиентом.

Зачем не google-genai SDK: один REST-метод не стоит SDK-зависимости
(конвенция проекта — см. calendar/api.py), а независимость от чужого кода —
часть отказоустойчивости fallback-пути. Собственных ретраев нет: это
последний рубеж, повторами управляет очередь сообщений.
ДЕНЬГИ: каждый extract() — платный вызов Gemini API (с боевым ключом).
Дефолтная модель — кандидат: подтвердить eval'ом (шаг 2) до боевого включения.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx
from pydantic import ValidationError

from navbat.nlu.extractor import ExtractionError, ProviderDownError
from navbat.nlu.schema import Extraction

log = logging.getLogger("navbat.nlu")

DEFAULT_MODEL = "gemini-2.5-flash"
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_PROMPT_PATH = Path(__file__).parent / "prompts" / "system.md"
REPAIR_TRIES = 1
LLM_TIMEOUT = 8.0  # секунд — как у основного провайдера (BRIEF)

# OpenAPI-subset для responseSchema: enum'ы intent/service/language жёсткие,
# словари date_ref/time_ref — regex, их доваливает pydantic-валидатор схемы
# (как и у OpenAI: union в structured outputs не выразить).
_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "intent": {"type": "STRING",
                   "enum": ["book", "reschedule", "cancel", "question", "other"]},
        "service": {"type": "STRING", "nullable": True,
                    "enum": ["cleaning", "filling", "extraction", "implant",
                             "crown", "whitening", "braces", "checkup", "xray"]},
        "doctor": {"type": "STRING", "nullable": True},
        "date_ref": {"type": "STRING", "nullable": True},
        "time_ref": {"type": "STRING", "nullable": True},
        "language": {"type": "STRING", "enum": ["uz", "ru", "mixed"]},
        "is_medical": {"type": "BOOLEAN"},
    },
    "required": ["intent", "service", "doctor", "date_ref", "time_ref",
                 "language", "is_medical"],
}


class GeminiExtractor:
    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        on_usage=None,
        on_repair=None,
        client: httpx.Client | None = None,
        prompt: str | None = None,
    ) -> None:
        self._model = model or os.environ.get("NAVBAT_GEMINI_MODEL", DEFAULT_MODEL)
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self._on_usage = on_usage  # callable(in_tokens, out_tokens) — учёт бюджета
        self._on_repair = on_repair  # callable() — метрика NLU-дрифта
        self._client = client or httpx.Client(timeout=LLM_TIMEOUT)
        # prompt — версия из БД (B.2); None → встроенный файл
        self._system_prompt = prompt or _PROMPT_PATH.read_text(encoding="utf-8")

    def extract(self, text: str) -> Extraction:
        contents = [{"role": "user", "parts": [{"text": text}]}]
        last_error: ValidationError | None = None
        for attempt in range(REPAIR_TRIES + 1):
            raw = self._generate(contents)
            try:
                return Extraction.model_validate_json(raw)
            except ValidationError as e:
                last_error = e
                log.warning("NLU(gemini): невалидный ответ (попытка %d): %s",
                            attempt + 1, e)
                if self._on_repair and attempt < REPAIR_TRIES:
                    self._on_repair()  # считаем только реальные повторы
                # repair полезен, только если модель видит, ЧТО не прошло
                contents.append({"role": "model", "parts": [{"text": raw}]})
                contents.append({"role": "user", "parts": [{"text":
                    f"Твой прошлый ответ не прошёл валидацию схемы: "
                    f"{str(e)[:300]}. Верни исправленный JSON строго "
                    f"по допустимым значениям."}]})
        raise ExtractionError(
            f"NLU(gemini) не дал валидный JSON после repair: {last_error}")

    def _generate(self, contents: list[dict]) -> str:
        """Один вызов generateContent → текст ответа модели."""
        body = {
            "system_instruction": {"parts": [{"text": self._system_prompt}]},
            "contents": contents,
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": 2000,
                "responseMimeType": "application/json",
                "responseSchema": _RESPONSE_SCHEMA,
            },
        }
        try:
            response = self._client.post(
                f"{BASE_URL}/models/{self._model}:generateContent",
                json=body,
                # ключ в заголовке, не в query: не светить в логах/трейсах
                headers={"x-goog-api-key": self._api_key},
            )
        except httpx.TransportError as e:
            raise ProviderDownError(f"gemini: сеть/таймаут: {e}") from e
        if response.status_code != 200:
            raise ProviderDownError(
                f"gemini: HTTP {response.status_code}: {response.text[:200]}")
        payload = response.json()
        if self._on_usage and "usageMetadata" in payload:
            meta = payload["usageMetadata"]
            self._on_usage(meta.get("promptTokenCount", 0),
                           meta.get("candidatesTokenCount", 0))
        candidates = payload.get("candidates") or []
        parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
        if not parts or "text" not in parts[0]:
            # safety-блок/пустой ответ: модель жива, ответа нет — repair-hint
            # без ответа бессмыслен, сразу сбой NLU (в отличие от кривого JSON)
            raise ExtractionError(f"gemini: пустой ответ: {str(payload)[:200]}")
        return parts[0]["text"]
