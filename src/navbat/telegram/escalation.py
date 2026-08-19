"""Эскалация человеку через Telegram-чат админа клиники (P0 BRIEF).

Реализует EscalationNotifier (dialog/escalation.py). Сбой доставки не
роняет обработку пациента: эскалация — сигнал, не транзакция.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime

from navbat.dialog.replies import service_label
from navbat.telegram.admin_texts import DEFAULT_LANG, Reason, plain, render_reason
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


def summarize_context(context: dict, lang: str = DEFAULT_LANG) -> str:
    """Читаемая для админа выжимка брони из контекста эскалации (M3):
    что пациент успел выбрать. Внутренние флаги (lang, счётчики) и PII
    (имя — уже вырезано m1) не показываем.

    Язык — админ-чата, а не пациента: выжимка стоит в том же сообщении, что
    причина, и русские подписи полей рядом с узбекской шапкой выглядели
    полупереводом (остаток по №16)."""
    fields = (("service", "ctx_service", lambda v: service_label(v, lang)),
              ("date", "ctx_day", _fmt_date),
              ("time_ref", "ctx_time", str),
              ("slot_start", "ctx_slot", _fmt_dt),
              ("slot_doctor", "ctx_doctor", str),
              ("cancel_when", "ctx_cancel", str))
    parts = [plain(key, lang, value=fmt(context[field]))
             for field, key, fmt in fields if context.get(field)]
    return "; ".join(parts) if parts else plain("ctx_empty", lang)


def _reason_in(reason, lang: str) -> str:
    """Причина на языке получателя.

    Служебный путь мог отдать Reason (ключ + подстановки) — тогда переводим;
    обычная строка уходит как есть: системные алерты про cert, бэкапы и webhook
    читает тот, кто чинит, и переводить их некому (остаток по №16).

    Рендер намеренно fail-open (render_reason): сломанный шаблон гасил рассылку
    целиком — ни этот получатель, ни следующие не узнавали о сбое (ревью)."""
    if isinstance(reason, Reason):
        return render_reason(reason.key, lang, reason.params)
    return str(reason)


class TelegramEscalation:
    """Шлёт алерт ВСЕМ админ-чатам клиники (M4). Веер скрыт здесь — вызыватели
    просто зовут notify(), не зная про список."""

    def __init__(self, api, admin_chat_id=None, lang_of=None) -> None:
        self._api = api
        self._admin_chat_ids = _as_chat_tuple(admin_chat_id)
        # язык конкретного админ-чата (карта, №16): алерт приходит владельцу
        # на том же языке, на котором он держит консоль. Без резолвера —
        # русский, чтобы CLI и тесты не тянули за собой БД
        self._lang_of = lang_of or (lambda chat: DEFAULT_LANG)
        # владелец системы (не клиники): системные алерты дублируются ему
        raw_owner = os.environ.get("NAVBAT_OWNER_CHAT_ID", "")
        self._owner_chat = int(raw_owner) if raw_owner.lstrip("-").isdigit() else None

    def notify(self, chat_id: int, reason: str, context: dict) -> None:
        if not self._admin_chat_ids:
            log.warning("эскалация chat=%s (админ-чаты не заданы): %s | %s",
                        chat_id, reason, context)
            return
        for admin_chat in self._admin_chat_ids:
            lang = self._lang_of(admin_chat)
            message = plain("alert_escalation", lang,
                            chat=chat_id, reason=_reason_in(reason, lang),
                            context=summarize_context(context, lang))
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
        for admin_chat in self._admin_chat_ids:
            lang = self._lang_of(admin_chat)
            message = plain("alert_fyi", lang,
                            reason=_reason_in(reason, lang),
                            context=summarize_context(context, lang))
            try:
                self._api.send_message(admin_chat, message)
            except TelegramAPIError as e:
                log.error("FYI chat=%s не доставлен админу %s: %s | %s",
                          chat_id, admin_chat, e, reason)

    def notify_ops(self, reason: str, context: dict,
                   detail: str | None = None) -> None:
        """Операционный сигнал: админ-чаты клиники + владелец системы.

        Клинике — только причина (ей с ней работать), владельцу — причина
        плюс техническая часть. Так администратор узнаёт, что синк стоит
        или пациенту не дошло напоминание, но покупатель на показе не
        читает текст исключения (ревью волны B, блокер 2)."""
        targets = list(self._admin_chat_ids)
        for chat in targets:
            # владелец системы часто и есть админ-чат клиники (пилот на одном
            # аккаунте) — ему техническая часть нужна, остальным нет
            lang = self._lang_of(chat)
            if chat == self._owner_chat:
                text = plain("alert_system", lang,
                             reason=_reason_in(reason, lang))
                if detail:
                    text += f"\n{detail}"
            else:
                text = plain("alert_ops", lang,
                             reason=_reason_in(reason, lang))
            self._send_alert(chat, text, reason)
        if self._owner_chat and self._owner_chat not in targets:
            owner_lang = self._lang_of(self._owner_chat)
            owner_text = plain("alert_system", owner_lang,
                               reason=_reason_in(reason, owner_lang))
            if detail:
                owner_text += f"\n{detail}"
            self._send_alert(self._owner_chat, owner_text, reason)
        if not targets and not self._owner_chat:
            log.warning("операционный алерт (чаты не заданы): %s | %s",
                        reason, context)

    def _send_alert(self, chat: int, message: str, reason: str) -> None:
        try:
            self._api.send_message(chat, message)
        except TelegramAPIError as e:
            log.error("алерт не доставлен в %s: %s | %s", chat, e, reason)

    def notify_system(self, reason: str, context: dict) -> None:
        """Системный алерт: владельцу системы, а клинике — только если
        канала владельца нет.

        Раньше шёл веером во все админ-чаты: на показе покупатель читал
        текст исключения в том же чате, что у него на экране (карта, №10).
        Фолбэк сохранён — потерять «бэкапы не снимаются» хуже, чем показать
        его клинике."""
        targets = ([self._owner_chat] if self._owner_chat
                   else list(self._admin_chat_ids))
        if not targets:
            log.warning("системный алерт (чаты не заданы): %s | %s",
                        reason, context)
            return
        for chat in targets:
            lang = self._lang_of(chat)
            message = plain("alert_system", lang,
                            reason=_reason_in(reason, lang))
            try:
                self._api.send_message(chat, message)
            except TelegramAPIError as e:
                log.error("системный алерт не доставлен в %s: %s | %s",
                          chat, e, reason)
