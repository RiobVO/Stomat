"""Каскад LLM-провайдеров: аутэйдж основного → запасной (BRIEF разд. 14.B).

Без запасного аутэйдж OpenAI ночью = все диалоги в dead letter, когда
человека нет. ExtractionError — НЕ повод для failover: модель жива, ответ
мусорный; запасной не спасёт, а латентность удвоит. Оба провайдера легли —
ProviderDownError наружу, апдейт ретраит очередь сообщений.
"""
from __future__ import annotations

import logging

from navbat.nlu.extractor import Extractor, ProviderDownError
from navbat.nlu.schema import Extraction

log = logging.getLogger("navbat.nlu")


class FallbackExtractor:
    def __init__(self, primary: Extractor, secondary: Extractor) -> None:
        self._primary = primary
        self._secondary = secondary

    def extract(self, text: str) -> Extraction:
        try:
            return self._primary.extract(text)
        except ProviderDownError as e:
            log.warning("основной LLM недоступен (%s) — переключаюсь на запасной", e)
            return self._secondary.extract(text)
