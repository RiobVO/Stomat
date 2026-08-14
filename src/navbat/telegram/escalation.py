"""Эскалация человеку через Telegram-чат админа клиники (P0 BRIEF).

Реализует EscalationNotifier (dialog/escalation.py). Сбой доставки не
роняет обработку пациента: эскалация — сигнал, не транзакция.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime

from navbat.dialog.replies import service_label
from navbat.telegram.api import TelegramAPIError

log = logging.getLogger("navbat.escalation")


def _as_chat_tuple(value) -> tuple[int, ...]:
    """Нормализует admin/digest-чаты к кортежу: принимаем None, int или
    список/массив (Postgres bigint[]). Back-compat со старым одиночным int."""
    if value is None:
        return ()
    if isinstance(value, int):
        return (value,)
    return tuple(value)


def _fmt_date(iso: str) -> str:
    try:
        return date.fromisoformat(iso).strftime("%d.%m")
    except (ValueError, TypeError):
        return str(iso)


def _fmt_dt(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m %H:%M")
    except (ValueError, TypeError):
        return str(iso)


def summarize_context(context: dict) -> str:
    """Читаемая для админа выжимка брони из контекста эскалации (M3):
    что пациент успел выбрать. Внутренние флаги (lang, счётчики) и PII
    (имя — уже вырезано m1) не показываем. Метки услуг — по-русски, админ
    читает по-русски."""
    parts: list[str] = []
    if context.get("service"):
        parts.append(f"услуга — {service_label(context['service'], 'ru')}")
    if context.get("date"):
        parts.append(f"день — {_fmt_date(context['date'])}")
    if context.get("time_ref"):
        parts.append(f"время — {context['time_ref']}")
    if context.get("slot_start"):
        parts.append(f"выбранный слот — {_fmt_dt(context['slot_start'])}")
    if context.get("slot_doctor"):
        parts.append(f"врач — {context['slot_doctor']}")
    if context.get("cancel_when"):
        parts.append(f"отмена записи на — {context['cancel_when']}")
    return "; ".join(parts) if parts else "пациент ещё ничего не выбрал"


class TelegramEscalation:
    """Шлёт алерт ВСЕМ админ-чатам клиники (M4). Веер скрыт здесь — вызыватели
    просто зовут notify(), не зная про список."""

    def __init__(self, api, admin_chat_id=None) -> None:
        self._api = api
        self._admin_chat_ids = _as_chat_tuple(admin_chat_id)
        # владелец системы (не клиники): системные алерты дублируются ему
        raw_owner = os.environ.get("NAVBAT_OWNER_CHAT_ID", "")
        self._owner_chat = int(raw_owner) if raw_owner.lstrip("-").isdigit() else None

    def notify(self, chat_id: int, reason: str, context: dict) -> None:
        if not self._admin_chat_ids:
            log.warning("эскалация chat=%s (админ-чаты не заданы): %s | %s",
                        chat_id, reason, context)
            return
        message = (f"Эскалация: чат {chat_id}\n"
                   f"Причина: {reason}\n"
                   f"Что хотел пациент: {summarize_context(context)}\n"
                   f"Снять: /release {chat_id}")
        for admin_chat in self._admin_chat_ids:
            try:
                self._api.send_message(admin_chat, message)
            except TelegramAPIError as e:
                log.error("эскалация chat=%s не доставлена админу %s: %s | %s",
                          chat_id, admin_chat, e, reason)

    def notify_fyi(self, chat_id: int, reason: str, context: dict) -> None:
        """🟡 Информирование владельца: человек не нужен, снимать нечего.

        Отличается от notify() отсутствием эскалационной шапки и подсказки
        /release — пациент не заморожен (карта продажи, №9)."""
        if not self._admin_chat_ids:
            log.info("FYI chat=%s (админ-чаты не заданы): %s | %s",
                     chat_id, reason, context)
            return
        message = (f"🟡 К сведению: {reason}\n"
                   f"Что хотел пациент: {summarize_context(context)}")
        for admin_chat in self._admin_chat_ids:
            try:
                self._api.send_message(admin_chat, message)
            except TelegramAPIError as e:
                log.error("FYI chat=%s не доставлен админу %s: %s | %s",
                          chat_id, admin_chat, e, reason)

    def notify_system(self, reason: str, context: dict) -> None:
        """Системный алерт: владельцу системы, а клинике — только если
        канала владельца нет.

        Раньше шёл веером во все админ-чаты: на показе покупатель читал
        текст исключения в том же чате, что у него на экране (карта, №10).
        Фолбэк сохранён — потерять «бэкапы не снимаются» хуже, чем показать
        его клинике."""
        message = f"⚠ Системный алерт\n{reason}"
        targets = ([self._owner_chat] if self._owner_chat
                   else list(self._admin_chat_ids))
        if not targets:
            log.warning("системный алерт (чаты не заданы): %s | %s",
                        reason, context)
            return
        for chat in targets:
            try:
                self._api.send_message(chat, message)
            except TelegramAPIError as e:
                log.error("системный алерт не доставлен в %s: %s | %s",
                          chat, e, reason)
