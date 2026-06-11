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


# FAQ-слой (П-2б): два самых частых бытовых вопроса бот закрывает сам,
# без LLM. «во сколько» — только в связке с работой/открытием, иначе
# ловит «во сколько меня записали».
_HOURS_RE = re.compile(
    r"(до\s+скольк|со\s+скольк|час\w*\s+работ|график|режим\w*\s+работ"
    r"|когда\s+(?:вы\s+)?(?:работа|открыва|закрыва)"
    r"|во\s+сколько\s+(?:вы\s+)?(?:работа|открыва|закрыва)"
    r"|ish\s+vaqti|qachongacha|soat\s+nechagacha"
    r"|qachon\s+(?:ochil|yopil|ishla))",
)
_ADDRESS_RE = re.compile(
    r"(адрес|где\s+вы|где\s+наход|как\s+(?:до\s+вас\s+)?(?:добраться|доехать"
    r"|пройти|найти)|куда\s+(?:подойти|прийти|приходить|ехать)"
    r"|manzil|qayerda|qanday\s+bor)",
)
# FAQ-темы полировки-2. Оплата: «карт-» только в падежах вопроса об оплате
# (картой/карта/карты…), «стоит/стоимость» сюда не относится — это прайс;
# uz «bo'lib» лишь в биграмме с to'l- («bo'lib to'lash» = рассрочка),
# одиночное bo'lib — служебный глагол («kasal bo'lib qoldim»).
_PAYMENT_RE = re.compile(
    r"\b(оплат\w*|рассрочк\w*|карт(?:ой|а|у|ы)|наличн\w*"
    r"|to'lov\w*|bo'lib\s+to'l\w*|karta|naqd)\b",
)
# Телефон: «номер» — только про клинику/«у вас» (иначе ловит «оставил номер
# соседу» и шаг контакта); «у вас номер» и «номер клиники» — оба порядка.
_PHONE_RE = re.compile(
    r"\b(телефон\w*|позвонить|дозвон\w*"
    r"|номер\s+(?:клиники|телефона)|у\s+вас\s+номер"
    r"|telefon\w*|qo'ng'iroq|raqam\w*\s+bormi)\b",
)


def mentions_hours_question(message: str) -> bool:
    """Вопрос о часах работы клиники (ru/uz)."""
    return _HOURS_RE.search(_normalize(message)) is not None


def mentions_address_question(message: str) -> bool:
    """Вопрос об адресе/как добраться (ru/uz)."""
    return _ADDRESS_RE.search(_normalize(message)) is not None


def mentions_payment_question(message: str) -> bool:
    """Вопрос об оплате/рассрочке (ru/uz)."""
    return _PAYMENT_RE.search(_normalize(message)) is not None


def mentions_phone_question(message: str) -> bool:
    """Вопрос о телефоне клиники / просьба позвонить (ru/uz)."""
    return _PHONE_RE.search(_normalize(message)) is not None


def _looks_like_question(message: str) -> bool:
    # ТОЛЬКО явный «?»: используется на PII-шагах (имя/телефон) для
    # прерывания вопросом вбок. Критерий длины убран — длинное ФИО без «?»
    # не вопрос, а раньше уходило в LLM (утечка PII, M2).
    return "?" in message


class SlotGuard(Protocol):
    """Финальная перепроверка слота во внешнем источнике (GCal) перед confirm."""

    def is_free(self, doctor_id: uuid.UUID, start: datetime, end: datetime) -> bool: ...
