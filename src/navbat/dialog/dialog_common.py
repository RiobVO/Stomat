"""Общие константы, утилита и протокол диалогового слоя — отдельный модуль,
чтобы mixin'ы сценариев и роутер (fsm.py) делили их без циклического импорта:
fsm.py импортирует mixin'ы, mixin'ы — этот модуль, не наоборот."""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Protocol

MAX_NLU_FAILURES = 2     # подряд; дальше — эскалация
SLOTS_PER_REPLY = 4      # кнопок со временем в одном ответе
NEAREST_DAY_SCAN = 14    # дней вперёд при поиске свободного дня

# Вопрос о наличии в любой формулировке («а ещё?», «другой день?») должен
# вести к выбору дня, не к эскалации (П-1, находка живого теста 10.06).
# Словарь узкий и с границами слов: «друг» (человек) не матчится, «друго-/
# други-» (другое время, другие дни) — да. Узбекский — латиница; апострофы
# нормализуются в _mentions (пациенты шлют ' ʻ ’ ` вперемешку).
_AVAILABILITY_RE = re.compile(
    r"\b(ещё|еще|друго\w*|други\w*|свободн\w*|окошк\w*|мест\w*|слот\w*"
    r"|вариант\w*|попозже|пораньше"
    r"|boshqa|yana|bo'sh\w*|joy\w*|vaqt\w*)\b",
)
_APOSTROPHES = ("ʻ", "’", "`")


def mentions_availability(message: str) -> bool:
    """Текст содержит явный маркер вопроса о наличии слотов (ru/uz)."""
    norm = message.casefold()
    for apo in _APOSTROPHES:
        norm = norm.replace(apo, "'")
    return _AVAILABILITY_RE.search(norm) is not None


def _looks_like_question(message: str) -> bool:
    # ТОЛЬКО явный «?»: используется на PII-шагах (имя/телефон) для
    # прерывания вопросом вбок. Критерий длины убран — длинное ФИО без «?»
    # не вопрос, а раньше уходило в LLM (утечка PII, M2).
    return "?" in message


class SlotGuard(Protocol):
    """Финальная перепроверка слота во внешнем источнике (GCal) перед confirm."""

    def is_free(self, doctor_id: uuid.UUID, start: datetime, end: datetime) -> bool: ...
