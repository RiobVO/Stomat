"""Общие константы, утилита и протокол диалогового слоя — отдельный модуль,
чтобы mixin'ы сценариев и роутер (fsm.py) делили их без циклического импорта:
fsm.py импортирует mixin'ы, mixin'ы — этот модуль, не наоборот."""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Protocol

MAX_NLU_FAILURES = 2     # подряд; дальше — «не понял» + повтор шага кнопками
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

# Прямая просьба позвать человека — единственный текстовый путь к эскалации
# (П-2а). «человек»/«odam» — только в биграммах: «запишите двух человек» и
# «ikki odamga» — это количество пациентов, не просьба оператора.
_HUMAN_REQUEST_RE = re.compile(
    r"\b(администратор\w*|оператор\w*|менеджер\w*"
    r"|позов\w*|позвать|соедин\w*"
    r"|жив(?:ой|ым)\s+человек\w*|нужен\s+человек|дайте\s+человека"
    r"|с\s+человеком"
    r"|administrator\w*|operator\w*|menejer\w*|chaqir\w*"
    r"|odam\s+kerak|odam\s+bilan|jonli\s+odam)\b",
)


def _normalize(message: str) -> str:
    norm = message.casefold()
    for apo in _APOSTROPHES:
        norm = norm.replace(apo, "'")
    return norm


def mentions_availability(message: str) -> bool:
    """Текст содержит явный маркер вопроса о наличии слотов (ru/uz)."""
    return _AVAILABILITY_RE.search(_normalize(message)) is not None


def mentions_human_request(message: str) -> bool:
    """Пациент прямо просит позвать человека (ru/uz)."""
    return _HUMAN_REQUEST_RE.search(_normalize(message)) is not None


def _looks_like_question(message: str) -> bool:
    # ТОЛЬКО явный «?»: используется на PII-шагах (имя/телефон) для
    # прерывания вопросом вбок. Критерий длины убран — длинное ФИО без «?»
    # не вопрос, а раньше уходило в LLM (утечка PII, M2).
    return "?" in message


class SlotGuard(Protocol):
    """Финальная перепроверка слота во внешнем источнике (GCal) перед confirm."""

    def is_free(self, doctor_id: uuid.UUID, start: datetime, end: datetime) -> bool: ...
