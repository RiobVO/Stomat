"""Эскалация человеку через Telegram-чат админа клиники (P0 BRIEF).

Реализует EscalationNotifier (dialog/escalation.py). Сбой доставки не
роняет обработку пациента: эскалация — сигнал, не транзакция.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import Callable

from navbat.db.base import tenant_transaction
from navbat.dialog.replies import service_label
from navbat.telegram import relay_repo
from navbat.telegram.admin_texts import (DEFAULT_LANG, Reason,
                                         admin_lang_resolver, at, plain,
                                         render_reason)
from navbat.telegram.api import TelegramAPIError

log = logging.getLogger("navbat.escalation")

# пометка «записано вне рабочих часов» в карточке ленты: эмодзи одинаково
# читается на обоих языках, поэтому живёт в коде, а не в шаблонах
NIGHT_MARK = " 🌙"


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

    def __init__(self, api, admin_chat_id=None, lang_of=None,
                 anchor_writer: Callable[[int, int, int], None] | None = None
                 ) -> None:
        self._api = api
        self._admin_chat_ids = _as_chat_tuple(admin_chat_id)
        # якорь свайп-ответа (админ-чат, message_id) → пациент; без писателя
        # (CLI, тесты без БД) канал ответа просто не заводится
        self._anchor_writer = anchor_writer
        # язык конкретного админ-чата (карта, №16): алерт приходит владельцу
        # на том же языке, на котором он держит консоль. Без резолвера —
        # русский, чтобы CLI и тесты не тянули за собой БД
        self._lang_of = lang_of or (lambda chat: DEFAULT_LANG)
        # владелец системы (не клиники): системные алерты дублируются ему
        raw_owner = os.environ.get("NAVBAT_OWNER_CHAT_ID", "")
        self._owner_chat = int(raw_owner) if raw_owner.removeprefix("-").isdecimal() else None

    def notify(self, chat_id: int, reason: str, context: dict) -> bool:
        """False — алерт не дошёл НИ ОДНОМУ админ-чату (транспортный сбой):
        кулдаун дублей в fsm по такому «алерту» не стартует. Отсутствие
        админ-чатов — штатная конфигурация, получатель — лог."""
        if not self._admin_chat_ids:
            log.warning("эскалация chat=%s (админ-чаты не заданы): %s | %s",
                        chat_id, reason, context)
            return True
        delivered = False
        for admin_chat in self._admin_chat_ids:
            lang = self._lang_of(admin_chat)
            message = plain("alert_escalation", lang,
                            chat=chat_id, reason=_reason_in(reason, lang),
                            context=summarize_context(context, lang))
            try:
                sent = self._api.send_message(admin_chat, message)
                delivered = True
            except TelegramAPIError as e:
                log.error("эскалация chat=%s не доставлена админу %s: %s | %s",
                          chat_id, admin_chat, e, reason)
                continue
            self._save_anchor(admin_chat, sent, chat_id)
        return delivered

    def _save_anchor(self, admin_chat: int, sent, patient_chat: int) -> None:
        """Якорь свайп-ответа на ЭТО сообщение админ-чата.

        Свой message_id на каждую отправку: он уникален внутри чата, один общий
        увёл бы ответ администратора не тому пациенту. Сбой записи ловим широко
        и рассылку не роняем — эскалация это сигнал, а не транзакция: потерять
        «пациент просит человека» хуже, чем потерять свайп-ответ (у админа
        остаются /release и прямой чат).
        """
        if self._anchor_writer is None:
            return
        message_id = (sent or {}).get("message_id")
        if message_id is None:
            return
        try:
            self._anchor_writer(admin_chat, message_id, patient_chat)
        except Exception as e:  # noqa: BLE001 — алерт важнее якоря
            log.error("якорь ответа не сохранён (админ %s, сообщение %s, "
                      "пациент %s): %r", admin_chat, message_id,
                      patient_chat, e)

    def relay_from_patient(self, chat_id: int, text: str) -> None:
        """💬 Реплика замороженного пациента всем админ-чатам.

        Не алерт: ни эскалационной шапки, ни подсказки /release — человек уже
        вызван, а второй алерт на каждую реплику ломал бы анти-спам. Якорь
        пишется на КАЖДУЮ карточку: администратор отвечает свайпом на
        последнюю реплику, а не отматывает чат до первого алерта.
        """
        if not self._admin_chat_ids:
            log.warning("реплика chat=%s не переслана (админ-чаты не заданы)",
                        chat_id)
            return
        for admin_chat in self._admin_chat_ids:
            lang = self._lang_of(admin_chat)
            message = plain("relay_card", lang, chat=chat_id, text=text)
            try:
                sent = self._api.send_message(admin_chat, message)
            except TelegramAPIError as e:
                # остальным получателям реплика всё равно нужна (паттерн
                # notify_fyi): сбой одного чата не гасит рассылку
                log.error("реплика chat=%s не доставлена админу %s: %s",
                          chat_id, admin_chat, e)
                continue
            self._save_anchor(admin_chat, sent, chat_id)

    def review_alert(self, chat_id: int, rating: int, service_key: str | None,
                     when: str) -> None:
        """⭐1–3 всем админ-чатам: перехват негатива до публичного отзыва.

        Не эскалация и не FYI: пациент не заморожен и человека не просил,
        поэтому ни шапки, ни подсказки /release здесь нет. Но якорь свайп-ответа
        пишется на КАЖДОЕ сообщение (в отличие от карточки ленты): смысл алерта
        в том, чтобы владелец ответил недовольному прямо отсюда, не разыскивая
        его чат руками.

        Уходит PLAIN, как остальные алерты: подставляются только число, chat_id
        и строки каталога — пациентского текста, который надо экранировать,
        в алерте нет.
        """
        if not self._admin_chat_ids:
            log.warning("оценка %s от chat=%s: алерт не отправлен "
                        "(админ-чаты не заданы)", rating, chat_id)
            return
        for admin_chat in self._admin_chat_ids:
            lang = self._lang_of(admin_chat)
            message = plain(
                "alert_review", lang, rating=rating, chat=chat_id, when=when,
                # услуги может не быть у записи с улицы — алерт это не отменяет;
                # заглушка та же, что в карточке ленты, чтобы владелец читал
                # про один и тот же приём одними словами
                service=(service_label(service_key, lang) if service_key
                         else plain("feed_no_service", lang)))
            try:
                sent = self._api.send_message(admin_chat, message)
            except Exception as e:  # noqa: BLE001 — алерт важнее одного чата
                # остальным получателям алерт всё равно нужен (паттерн
                # booking_event): сбой одного чата не гасит рассылку. Шире
                # TelegramAPIError намеренно — клиент бросает и ValueError
                # (httpx .json() на не-JSON ответе прокси, api._call)
                log.error("оценка %s от chat=%s не доставлена админу %s: %r",
                          rating, chat_id, admin_chat, e)
                continue
            self._save_anchor(admin_chat, sent, chat_id)

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

    def booking_event(self, kind: str, card, when: str,
                      night: bool = False) -> None:
        """Карточка ленты (✅/❌/🔄) всем админ-чатам в момент события.

        Не алерт и не FYI: реагировать не нужно, это витрина работы бота для
        клиники без Google Calendar. Якорь свайп-ответа не пишется — карточка
        не канал разговора с пациентом (в отличие от 💬-реплики).

        Уходит с parse_mode=HTML: подстановки экранирует at(), а имя пациента
        приходит из БД и голым «<» ломало бы парсер Telegram.
        """
        if not self._admin_chat_ids:
            log.info("лента: карточка %s не отправлена (админ-чаты не заданы)",
                     kind)
            return
        for admin_chat in self._admin_chat_ids:
            lang = self._lang_of(admin_chat)
            message = at(
                f"feed_{kind}", lang,
                patient=card.patient_name or at("feed_no_name", lang),
                service=(service_label(card.service_key, lang)
                         if card.service_key else at("feed_no_service", lang)),
                when=when,
                # врач без имени — штатное состояние каталога (onboard без
                # --doctor): та же заглушка, что в консоли
                doctor=card.doctor_name or at("doc_noname", lang),
                night=NIGHT_MARK if night else "",
            )
            try:
                self._api.send_message(admin_chat, message, parse_mode="HTML")
            except Exception as e:  # noqa: BLE001 — карточка некритична
                # остальным получателям карточка всё равно нужна (паттерн
                # notify_fyi): сбой одного чата не гасит рассылку. Шире
                # TelegramAPIError намеренно: клиент бросает и ValueError —
                # httpx .json() на не-JSON ответе прокси (api._call), до
                # всякого разбора ok:false, и это выбивало весь веер
                log.error("лента: карточка %s не доставлена админу %s: %r",
                          kind, admin_chat, e)

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


def build_escalation(tg_api, session_factory, clinic_id,
                     admin_chat_ids) -> TelegramEscalation:
    """Нотификатор, который ЗНАЕТ язык каждого админ-чата клиники.

    Единственная сборка для прода: резолвер легко забыть — standalone-синк
    (`python -m navbat.calendar`) собирал нотификатор сам и рассылал причины
    по-русски даже узбекскому владельцу (ревью). Тесты и CLI без БД создают
    TelegramEscalation напрямую и получают русский по умолчанию.

    Здесь же заводится писатель якорей: своей транзакцией, потому что якорь
    пишется ПОСЛЕ отправки — привязывать его к транзакции вызывающего значило
    бы откатывать канал ответа из-за постороннего сбоя."""
    def anchor_writer(admin_chat_id: int, message_id: int,
                      patient_chat_id: int) -> None:
        with tenant_transaction(session_factory, clinic_id) as session:
            relay_repo.save_anchor(session, admin_chat_id, message_id,
                                   patient_chat_id)

    return TelegramEscalation(tg_api, admin_chat_ids,
                              lang_of=admin_lang_resolver(session_factory,
                                                          clinic_id),
                              anchor_writer=anchor_writer)
