"""Диалоговый FSM: LLM — рот, код — мозг.

Все переходы и решения — детерминированный код; NLU только извлекает слоты
(за адаптером Extractor). Состояние живёт в таблице conversation и переживает
рестарт. Каждый handle_* — одна tenant-транзакция на разговор; занятость
решает SchedulingEngine своими транзакциями (его гарантии — exclusion
constraint + advisory lock, см. инкремент 1).

DialogEngine — роутер: входные точки, перехват /start и меню, маршрутизация
интента, callback-кнопки. Логика сценариев вынесена в mixin'ы
(booking_flow / reschedule_flow / cancel_flow) поверх общих хелперов
(shared_helpers); всё собирается в один объект, граф вызовов через self (R4).

Зашитые правила:
- book↔question бэкстоп: вопрос с date_ref/time_ref («есть время сегодня?») —
  это вопрос о наличии, отвечаем ВСЕГДА слотами (известная дыра NLU);
- is_medical=true → медицинский дисклеймер код-слоем, один раз за диалог;
- 2 кривых ответа NLU подряд → эскалация и стоп-состояние escalated.
"""
from __future__ import annotations

import uuid
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, sessionmaker

from navbat.db.base import tenant_transaction
from navbat.dialog import services_repo
from navbat.dialog.booking_flow import _BookingFlowMixin
from navbat.dialog.cancel_flow import _CancelFlowMixin
from navbat.dialog.conversation import (
    Conversation, load_conversation, save_conversation)
from navbat.dialog.dialog_common import MAX_NLU_FAILURES, SlotGuard
from navbat.dialog.escalation import EscalationNotifier, LoggingEscalation
from navbat.dialog.replies import (
    MEDICAL_DISCLAIMER,
    TEMPLATES,
    Button,
    Reply,
    menu_rows,
    service_label,
    t,
)
from navbat.dialog.reschedule_flow import _RescheduleFlowMixin
from navbat.dialog.shared_helpers import _SharedHelpersMixin
from navbat.nlu.extractor import ExtractionError, Extractor
from navbat.nlu.schema import Extraction
from navbat.scheduling.engine import SchedulingEngine
from navbat.scheduling.errors import AppointmentNotFoundError

_MENU_KEYS = ("btn_menu_book", "btn_menu_resched", "btn_menu_cancel",
              "btn_menu_prices", "btn_menu_lang")
# нажатие reply-кнопки приходит ТЕКСТОМ — матчим label'ы обоих языков
_MENU_ACTIONS = {
    TEMPLATES[key][lang]: key for key in _MENU_KEYS for lang in ("ru", "uz")
}


class DialogEngine(_SharedHelpersMixin, _BookingFlowMixin,
                   _RescheduleFlowMixin, _CancelFlowMixin):
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clinic_id: uuid.UUID,
        extractor: Extractor,
        notifier: EscalationNotifier | None = None,
        scheduler: SchedulingEngine | None = None,
        slot_guard: SlotGuard | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clinic_id = clinic_id
        self._extractor = extractor
        self._notifier = notifier or LoggingEscalation()
        self._sched = scheduler or SchedulingEngine(session_factory, clinic_id)
        self._slot_guard = slot_guard
        # источник «сейчас» (aware UTC); тесты инжектируют фиксированный момент
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._tz: ZoneInfo | None = None

    # ── Входные точки ────────────────────────────────────────────────────

    def handle_text(self, chat_id: int, message: str) -> Reply:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            conv = load_conversation(session, chat_id)
            reply = self._handle_command_or_menu(session, conv, message)
            if reply is None:
                first_contact = not conv.context.greeting_shown
                reply = self._process_text(session, conv, message)
                if first_contact:
                    # P0 BRIEF: дисклеймер при первом контакте
                    conv.context.greeting_shown = True
                    greeting = t("greeting", self._lang(conv),
                                 clinic=self._clinic_name(session))
                    # replace, не Reply(...): сохранить menu/contact_request
                    # ответа, иначе первый контакт стирает кнопки/запрос номера
                    reply = replace(reply, text=f"{greeting}\n\n{reply.text}")
            save_conversation(session, conv)
        return reply

    def handle_action(self, chat_id: int, action: str) -> Reply:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            conv = load_conversation(session, chat_id)
            reply = self._process_action(session, conv, action)
            save_conversation(session, conv)
        return reply

    def handle_contact(self, chat_id: int, phone: str, own: bool) -> Reply:
        """Контакт из кнопки «Поделиться»; own — собственный контакт отправителя."""
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            conv = load_conversation(session, chat_id)
            reply = self._process_contact(session, conv, phone, own)
            save_conversation(session, conv)
        return reply

    # ── /start и главное меню (перехват до NLU) ──────────────────────────

    def _handle_command_or_menu(self, session: Session, conv: Conversation,
                                message: str) -> Reply | None:
        """Перехват /start и label'ов reply-меню до NLU: ноль токенов.

        None — обычный текст, идёт штатным путём (LLM-fallback).
        Escalated пробивает только /start — пациент сам выходит из заморозки
        (BRIEF разд. 14.A); кнопки меню стоп-состояние не обходят.
        """
        if message.strip() == "/start":
            return self._on_start(session, conv)
        if conv.state == "escalated":
            return None  # стоп-состояние не обходится кнопками
        key = _MENU_ACTIONS.get(message.strip())
        if key is None:
            return None
        return self._on_menu(session, conv, key)

    def _on_start(self, session: Session, conv: Conversation) -> Reply:
        self._abort_pending(conv)
        # выход из заморозки = чистый счёт: иначе унаследованный счётчик ≥2
        # эскалировал бы повторно с первого же сбоя NLU
        conv.context.nlu_failures = 0
        if conv.context.lang is None:
            return self._lang_screen(conv)
        return self._greeting_with_menu(session, conv)

    def _lang_screen(self, conv: Conversation) -> Reply:
        return Reply(t("choose_lang", self._lang(conv)),
                     (Button(t("btn_lang_uz", "ru"), "lang:uz"),
                      Button(t("btn_lang_ru", "ru"), "lang:ru")))

    def _greeting_with_menu(self, session: Session, conv: Conversation) -> Reply:
        lang = self._lang(conv)
        conv.context.greeting_shown = True
        greeting = t("greeting", lang, clinic=self._clinic_name(session))
        return Reply(f"{greeting}\n\n{t('menu_hint', lang)}", menu=menu_rows(lang))

    def _abort_pending(self, conv: Conversation) -> bool:
        """Меню/старт посреди сценария = явная смена намерения.

        Висящий hold отпускаем: бронь слота не должна переживать отказ
        от записи. Возвращает True, если живой hold действительно отменён
        (протухший/уже отменённый — нет: цель и так была достигнута).
        """
        cancelled = False
        appt = conv.context.appointment_id
        if appt:
            with suppress(AppointmentNotFoundError):
                self._sched.cancel(uuid.UUID(appt))
                cancelled = True  # достигается только если cancel не бросил
        self._clear_booking(conv)
        conv.state = "idle"
        return cancelled

    def _on_menu(self, session: Session, conv: Conversation, key: str) -> Reply:
        """Диспетчер нажатий reply-кнопок главного меню."""
        if key == "btn_menu_book":
            self._abort_pending(conv)
            return self._advance_booking(session, conv)
        if key == "btn_menu_resched":
            self._abort_pending(conv)
            return self._start_reschedule(session, conv,
                                          self._empty_extraction(conv, "reschedule"))
        if key == "btn_menu_cancel":
            # «Отменить» посреди оформления = отказ от него: hold отпущен,
            # отвечаем «запись отменена» сразу — подтверждать нечего
            if self._abort_pending(conv):
                return Reply(t("cancel_done", self._lang(conv)))
            return self._start_cancel(session, conv)
        if key == "btn_menu_prices":
            return self._with_reprompt(session, conv,
                                       self._price_list(session, conv))
        return self._lang_screen(conv)  # btn_menu_lang

    def _empty_extraction(self, conv: Conversation, intent: str) -> Extraction:
        """Кнопка меню = чистый intent без слотов (минуя NLU)."""
        lang = self._lang(conv)
        # Extraction.language принимает только ru/uz/mixed; mixed здесь невозможен
        return Extraction(intent=intent, service=None, doctor=None,
                          date_ref=None, time_ref=None,
                          language=lang, is_medical=False)

    # ── Текст ────────────────────────────────────────────────────────────

    def _process_text(self, session: Session, conv: Conversation, message: str) -> Reply:
        lang = self._lang(conv)
        if conv.state == "escalated":
            return Reply(t("escalated", lang))
        if conv.state == "awaiting_name":
            return self._on_name(session, conv, message)
        if conv.state == "awaiting_phone":
            return self._on_phone(session, conv, message)

        try:
            extraction = self._extractor.extract(message)
        except ExtractionError:
            return self._on_nlu_failure(conv)
        conv.context.nlu_failures = 0
        lang = "ru" if extraction.language == "mixed" else extraction.language
        conv.context.lang = lang

        reply = self._route_intent(session, conv, extraction)
        return self._with_medical_disclaimer(conv, extraction, reply)

    def _route_intent(self, session: Session, conv: Conversation,
                      extraction: Extraction) -> Reply:
        # бэкстоп: «есть время сегодня?» NLU уводит в question — но вопрос
        # с привязкой ко времени == вопрос о наличии, отвечаем слотами
        booking_like = extraction.intent == "book" or (
            extraction.intent == "question"
            and (extraction.date_ref or extraction.time_ref)
        )
        if booking_like and conv.state == "resched_offer_slots":
            # уточнение даты внутри переноса — это всё ещё перенос
            self._merge_when(session, conv.context, extraction)
            return self._offer_resched_slots(session, conv)
        if booking_like:
            if extraction.intent == "question" and not extraction.service \
                    and not conv.context.service \
                    and self._service_id(session, "checkup") is not None:
                # наличие спрашивают без услуги — сетку считаем по осмотру
                conv.context.service = "checkup"
            return self._continue_booking(session, conv, extraction)
        if extraction.intent == "question":
            answer = self._answer_question(session, conv, extraction)
            return self._with_reprompt(session, conv, answer)
        if extraction.intent == "reschedule":
            return self._start_reschedule(session, conv, extraction)
        if extraction.intent == "cancel":
            return self._start_cancel(session, conv)
        lang = self._lang(conv)
        # off-topic не оставляет пациента без выхода — кнопки меню под рукой (M7)
        return Reply(t("other_fallback", lang), menu=menu_rows(lang))

    def _on_nlu_failure(self, conv: Conversation) -> Reply:
        lang = self._lang(conv)
        failures = conv.context.nlu_failures + 1
        conv.context.nlu_failures = failures
        if failures >= MAX_NLU_FAILURES:
            self._notifier.notify(conv.chat_id, "2 кривых ответа NLU подряд",
                                  self._escalation_context(conv))
            conv.state = "escalated"
            return Reply(t("escalated", lang))
        # не понятому пациенту всегда доступны кнопки самообслуживания (M7)
        return Reply(t("reask", lang), menu=menu_rows(lang))

    def _with_medical_disclaimer(self, conv: Conversation, extraction: Extraction,
                                 reply: Reply) -> Reply:
        if extraction.is_medical and not conv.context.medical_shown:
            conv.context.medical_shown = True
            disclaimer = MEDICAL_DISCLAIMER[self._lang(conv)]
            return Reply(f"{disclaimer}\n\n{reply.text}", reply.buttons)
        return reply

    # ── Кнопки (callback-actions) ────────────────────────────────────────

    def _process_action(self, session: Session, conv: Conversation, action: str) -> Reply:
        lang = self._lang(conv)
        if conv.state == "escalated":
            return Reply(t("escalated", lang))
        kind, _, rest = action.partition(":")
        if kind == "lang":
            conv.context.lang = rest
            if not conv.context.greeting_shown:
                return self._greeting_with_menu(session, conv)
            # посреди сценария _with_reprompt отдаёт inline-кнопки шага, и menu
            # из note теряется — reply_markup в Telegram один. Reply-клавиатура
            # перерисуется на новом языке со следующим menu-сообщением.
            note = Reply(t("lang_changed", rest), menu=menu_rows(rest))
            return self._with_reprompt(session, conv, note)
        if kind == "service":
            conv.context.service = rest
            return self._advance_booking(session, conv)
        if kind == "date":
            conv.context.date = rest
            if conv.context.resched_id:
                return self._offer_resched_slots(session, conv)
            return self._advance_booking(session, conv)
        if kind == "ask_date":
            if not conv.context.resched_id:
                conv.state = "booking_collect"
            return Reply(t("ask_date", lang), self._date_buttons(session, lang))
        if kind == "slot":
            doctor_id, _, start_iso = rest.partition(":")
            return self._on_slot_chosen(session, conv, doctor_id, start_iso)
        if kind == "reslot":
            return self._on_reslot(session, conv, rest)
        if kind == "cancel_yes":
            return self._on_cancel_confirmed(session, conv)
        if kind == "cancel_no":
            self._clear_booking(conv)
            conv.state = "idle"
            return Reply(t("cancel_kept", lang))
        if kind == "stale":
            # нажата кнопка из устаревшего сообщения — повторяем текущий шаг
            return self._with_reprompt(session, conv, Reply(t("stale_button", lang)))
        if kind == "attend":
            # кнопка «Приду» из напоминания — просто подтверждение
            return Reply(t("attend_ok", lang))
        if kind == "remind_cancel":
            return self._start_cancel_by_id(session, conv, rest)
        return Reply(t("other_fallback", lang), menu=menu_rows(lang))

    # ── Вопросы ──────────────────────────────────────────────────────────

    def _price_list(self, session: Session, conv: Conversation) -> Reply:
        """Весь прайс из каталога services; пустой каталог — к администратору."""
        lang = self._lang(conv)
        rows = services_repo.price_list(session)
        if not rows:
            return Reply(t("price_empty", lang))
        lines = []
        for row in rows:
            label = service_label(row.name, lang)
            if row.price is None:
                lines.append(t("price_line_unknown", lang, service=label))
            else:
                price = f"{int(row.price):,}".replace(",", " ")
                lines.append(t("price_line", lang, service=label, price=price))
        return Reply(t("price_header", lang) + "\n" + "\n".join(lines))

    def _answer_question(self, session: Session, conv: Conversation,
                         extraction: Extraction) -> Reply:
        """Цена — из каталога; всё прочее (адрес, часы…) — администратору.
        Состояние диалога вопрос не меняет."""
        lang = self._lang(conv)
        if extraction.service:
            label = service_label(extraction.service, lang)
            price = services_repo.service_price(session, extraction.service)
            if price is None:
                return Reply(t("price_unknown", lang, service=label))
            formatted = f"{int(price):,}".replace(",", " ")
            return Reply(t("price_answer", lang, service=label, price=formatted))
        self._notifier.notify(conv.chat_id, "вопрос вне компетенции бота",
                              self._escalation_context(conv))
        return Reply(t("faq_fallback", lang))

    def _with_reprompt(self, session: Session, conv: Conversation,
                       answer: Reply) -> Reply:
        """Прерывание вбок: ответ + повтор текущего шага, состояние не сброшено."""
        if conv.state in ("booking_collect", "booking_offer_slots"):
            prompt = self._advance_booking(session, conv)
        elif conv.state == "resched_offer_slots":
            prompt = self._offer_resched_slots(session, conv)
        elif conv.state == "cancel_confirm":
            prompt = self._cancel_prompt(conv)
        else:
            return answer
        return Reply(f"{answer.text}\n\n{prompt.text}", prompt.buttons)
