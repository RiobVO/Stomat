"""Эскалация человеку — интерфейс. Реальная доставка в TG-чат админа — инкремент 3."""
from __future__ import annotations

import logging
from typing import Protocol
from zoneinfo import ZoneInfo

from navbat.dialog import clinic_repo
from navbat.telegram import feed_repo

log = logging.getLogger("navbat.escalation")


class EscalationNotifier(Protocol):
    # False — алерт точно не доставлен ни одному получателю; None/True —
    # считаем доставленным (обратная совместимость фейков и лог-заглушки)
    def notify(self, chat_id: int, reason: str, context: dict) -> bool | None: ...


class LoggingEscalation:
    """Заглушка: фиксирует эскалацию в лог, пока нет канала до админа."""

    def notify(self, chat_id: int, reason: str, context: dict) -> None:
        log.warning("эскалация chat=%s: %s | контекст: %s", chat_id, reason, context)


def fyi_alert(notifier, chat_id: int, reason: str, context: dict) -> None:
    """🟡 FYI владельцу: событие произошло, человек не нужен и пациент НЕ
    заморожен (нет слотов две недели, откат ручной правки календаря).

    Отдельный канал от notify(): эскалационная шапка со «Снять: /release»
    вводила владельца в заблуждение и противоречила «эскалаций: 0» в /stats
    (карта продажи, №9). Нотификаторы без notify_fyi (фейки, LoggingEscalation)
    получают обычный notify — сигнал не теряется."""
    handler = getattr(notifier, "notify_fyi", None)
    if handler is not None:
        handler(chat_id, reason, context)
    else:
        notifier.notify(chat_id, reason, context)


def relay_from_patient(notifier, chat_id: int, text: str) -> None:
    """💬 Реплика замороженного пациента в админ-чаты — вход канала ответа.

    Не эскалация: фолбэка на notify() здесь нет (в отличие от fyi_alert) —
    он повторял бы алерт «пациент просит человека» на каждой реплике, ровно
    против анти-спама эскалаций. Нотификаторы без канала (LoggingEscalation,
    фейки) молчат: пациенту это ничего не ломает, он и так заморожен.

    Текст пациента в лог не пишем — это свободная реплика, в ней бывает имя
    (m1: PII не уходит в логи вместе с эскалацией)."""
    handler = getattr(notifier, "relay_from_patient", None)
    if handler is None:
        log.info("реплика chat=%s не переслана: канал ответа не настроен",
                 chat_id)
        return
    handler(chat_id, text)


def booking_feed(notifier, session, appointment_id, kind: str) -> None:
    """Карточка ленты (kind: booked | cancelled | resched) в админ-чаты.

    Клиника без Google Calendar не видит записи бота вообще — лента и есть
    её экран. Не эскалация: фолбэка на notify() здесь нет (как у
    relay_from_patient) — нотификаторы без канала (LoggingEscalation, фейки)
    молчат, будить человека карточкой не за чем.

    session уже открыт вызывающим: карточка собирается ровно из того
    состояния записи, которое он только что зафиксировал.
    """
    handler = getattr(notifier, "booking_event", None)
    if handler is None:
        log.debug("лента: карточка %s не отправлена — канал не настроен", kind)
        return
    # лента — сигнал, а не транзакция (как якорь свайп-ответа): сбой рендера
    # или доставки не вправе уронить обработку пациента. Он уже получил ответ
    # о записи, а повтор апдейта прогнал бы весь сценарий заново
    try:
        # savepoint: карточка собирается ВНУТРИ транзакции вызывающего, и
        # упавший SELECT травит её целиком (PostgreSQL 25P02) — дальше падает
        # всё, что диалог ещё не записал (привязка пациента, conversation),
        # хотя запись уже подтверждена своей транзакцией движка. Откат до
        # savepoint снимает abort и оставляет внешнюю транзакцию живой
        with session.begin_nested():
            card = feed_repo.appointment_card(session, appointment_id)
            if card is None:
                log.warning("лента: запись %s не найдена — карточка %s "
                            "пропущена", appointment_id, kind)
                return
            tz = ZoneInfo(clinic_repo.clinic_timezone(session))
            handler(kind, card, f"{card.start.astimezone(tz):%d.%m %H:%M}",
                    feed_repo.is_after_hours(session, card.created_at, tz))
    except Exception as e:  # noqa: BLE001 — карточка дешевле обработки пациента
        log.error("лента: карточка %s по записи %s не отправлена: %r",
                  kind, appointment_id, e)


def review_alert(notifier, session, appointment_id, chat_id: int,
                 rating: int) -> None:
    """⭐1–3 в админ-чаты: владелец перехватывает недовольного до того, как тот
    напишет публичный отзыв.

    Не эскалация: фолбэка на notify() здесь нет (как у booking_feed) —
    нотификаторы без канала (LoggingEscalation, фейки) молчат. Замораживать
    пациента и звать администратора никто не просил: это сигнал владельцу,
    решение звонить — его.

    session уже открыт вызывающим (транзакция тапа): услугу и время приёма
    добираем из неё же — оценка без «по какому приёму» владельцу бесполезна.
    """
    handler = getattr(notifier, "review_alert", None)
    if handler is None:
        log.debug("оценка %s по записи %s: канал алерта не настроен",
                  rating, appointment_id)
        return
    # алерт — сигнал, а не транзакция: сбой сбора данных или доставки не вправе
    # уронить обработку пациента, он свою оценку уже поставил
    try:
        # savepoint по той же причине, что у booking_feed: SELECT идёт ВНУТРИ
        # транзакции тапа, и его падение травит её целиком (PostgreSQL 25P02) —
        # вместе с ещё не закоммиченной оценкой и conversation пациента. Откат
        # до savepoint снимает abort и оставляет внешнюю транзакцию живой
        with session.begin_nested():
            card = feed_repo.appointment_card(session, appointment_id)
            if card is None:
                log.warning("оценка %s: запись %s не найдена — алерт пропущен",
                            rating, appointment_id)
                return
            tz = ZoneInfo(clinic_repo.clinic_timezone(session))
            handler(chat_id, rating, card.service_key,
                    f"{card.start.astimezone(tz):%d.%m %H:%M}")
    except Exception as e:  # noqa: BLE001 — алерт дешевле обработки пациента
        log.error("оценка %s по записи %s: алерт владельцу не отправлен: %r",
                  rating, appointment_id, e)


def ops_alert(notifier, reason: str, context: dict, chat_id: int = 0,
              detail: str | None = None) -> None:
    """Операционный сигнал КЛИНИКЕ (и владельцу системы заодно): бот
    что-то не смог, и с этим должен работать администратор — напоминание
    не доставлено, синк календаря стоит, дневной лимит токенов исчерпан,
    сообщение пациента не обработано.

    Отличается от system_alert адресатом: инфраструктура (cert, бэкапы,
    webhook, NLU-дрифт) клинике бесполезна, а эти четыре — её работа
    (ревью волны B, блокер 2). detail — техническая часть (текст
    исключения): уходит только владельцу, клиника читает причину
    человеческим языком (карта продажи, №10)."""
    handler = getattr(notifier, "notify_ops", None)
    if handler is not None:
        handler(reason, context, detail=detail)
    else:
        # у фолбэка (логи, фейки тестов) адресата разделять не на кого —
        # техническая часть не должна потеряться при диагностике
        notifier.notify(chat_id, f"{reason} ({detail})" if detail else reason,
                        context)


def system_alert(notifier, reason: str, context: dict, chat_id: int = 0) -> None:
    """Системный алерт (не пациентская эскалация): cert, синк, cap, дрифт,
    dead-letter. TelegramEscalation шлёт его и владельцу системы; нотификаторы
    без notify_system (фейки, LoggingEscalation) получают обычный notify."""
    handler = getattr(notifier, "notify_system", None)
    if handler is not None:
        handler(reason, context)
    else:
        notifier.notify(chat_id, reason, context)
