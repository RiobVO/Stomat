"""Общие константы, утилита и протокол диалогового слоя — отдельный модуль,
чтобы mixin'ы сценариев и роутер (fsm.py) делили их без циклического импорта:
fsm.py импортирует mixin'ы, mixin'ы — этот модуль, не наоборот."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Protocol

MAX_NLU_FAILURES = 2     # подряд; дальше — эскалация
SLOTS_PER_REPLY = 4      # кнопок со временем в одном ответе
NEAREST_DAY_SCAN = 14    # дней вперёд при поиске свободного дня


def _looks_like_question(message: str) -> bool:
    # ТОЛЬКО явный «?»: используется на PII-шагах (имя/телефон) для
    # прерывания вопросом вбок. Критерий длины убран — длинное ФИО без «?»
    # не вопрос, а раньше уходило в LLM (утечка PII, M2).
    return "?" in message


class SlotGuard(Protocol):
    """Финальная перепроверка слота во внешнем источнике (GCal) перед confirm."""

    def is_free(self, doctor_id: uuid.UUID, start: datetime, end: datetime) -> bool: ...
