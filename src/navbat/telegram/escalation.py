"""Эскалация человеку через Telegram-чат админа клиники (P0 BRIEF).

Реализует EscalationNotifier (dialog/escalation.py). Сбой доставки не
роняет обработку пациента: эскалация — сигнал, не транзакция.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from navbat.dialog.replies import service_label
from navbat.telegram.api import TelegramAPIError

log = logging.getLogger("navbat.escalation")


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
    def __init__(self, api, admin_chat_id: int | None) -> None:
        self._api = api
        self._admin_chat_id = admin_chat_id

    def notify(self, chat_id: int, reason: str, context: dict) -> None:
        if self._admin_chat_id is None:
            log.warning("эскалация chat=%s (admin_chat_id не задан): %s | %s",
                        chat_id, reason, context)
            return
        message = (f"Эскалация: чат {chat_id}\n"
                   f"Причина: {reason}\n"
                   f"Что хотел пациент: {summarize_context(context)}\n"
                   f"Снять: /release {chat_id}")
        try:
            self._api.send_message(self._admin_chat_id, message)
        except TelegramAPIError as e:
            log.error("эскалация chat=%s не доставлена админу: %s | %s",
                      chat_id, e, reason)
