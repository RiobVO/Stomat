"""Диалоговый FSM: LLM — рот, код — мозг.

Все переходы и решения — детерминированный код; NLU только извлекает слоты
(за адаптером Extractor). Состояние живёт в таблице conversation и переживает
рестарт. Каждый handle_* — одна tenant-транзакция на разговор; занятость
решает SchedulingEngine своими транзакциями (его гарантии — exclusion
constraint + advisory lock, см. инкремент 1).

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
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from navbat.crypto import decrypt_text
from navbat.db.base import tenant_transaction
from navbat.dialog.conversation import Conversation, load_conversation, save_conversation
from navbat.dialog.dates import matches_time_ref, resolve_date_ref
from navbat.dialog.escalation import EscalationNotifier, LoggingEscalation
from navbat.dialog.patients import create_patient, find_patient_by_chat, normalize_phone
from navbat.dialog.replies import (
    MEDICAL_DISCLAIMER,
    TEMPLATES,
    Button,
    Reply,
    menu_rows,
    service_label,
    t,
)
from navbat.nlu.extractor import ExtractionError, Extractor
from navbat.nlu.schema import Extraction
from navbat.scheduling.calendar_rules import open_bounds
from navbat.scheduling.engine import SchedulingEngine
from navbat.scheduling.errors import (
    AppointmentNotFoundError,
    HoldExpiredError,
    InvalidSlotError,
    SlotTakenError,
)

def _looks_like_question(message: str) -> bool:
    # ТОЛЬКО явный «?»: используется на PII-шагах (имя/телефон) для
    # прерывания вопросом вбок. Критерий длины убран — длинное ФИО без «?»
    # не вопрос, а раньше уходило в LLM (утечка PII, M2).
    return "?" in message


MAX_NLU_FAILURES = 2     # подряд; дальше — эскалация
SLOTS_PER_REPLY = 4      # кнопок со временем в одном ответе
NEAREST_DAY_SCAN = 14    # дней вперёд при поиске свободного дня

# ключи контекста, относящиеся к текущему сценарию (чистятся по завершении)
_BOOKING_KEYS = ("service", "date", "time_ref", "doctor_id", "doctor_miss",
                 "appointment_id", "slot_start", "slot_doctor", "pending_name",
                 "resched_id", "resched_doctor", "cancel_id", "cancel_when",
                 "cancel_via")

_MENU_KEYS = ("btn_menu_book", "btn_menu_resched", "btn_menu_cancel",
              "btn_menu_prices", "btn_menu_lang")
# нажатие reply-кнопки приходит ТЕКСТОМ — матчим label'ы обоих языков
_MENU_ACTIONS = {
    TEMPLATES[key][lang]: key for key in _MENU_KEYS for lang in ("ru", "uz")
}


class SlotGuard(Protocol):
    """Финальная перепроверка слота во внешнем источнике (GCal) перед confirm."""

    def is_free(self, doctor_id: uuid.UUID, start: datetime, end: datetime) -> bool: ...


class DialogEngine:
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
                first_contact = "greeting_shown" not in conv.context
                reply = self._process_text(session, conv, message)
                if first_contact:
                    # P0 BRIEF: дисклеймер при первом контакте
                    conv.context["greeting_shown"] = True
                    greeting = t("greeting", self._lang(conv),
                                 clinic=self._clinic_name(session))
                    reply = Reply(f"{greeting}\n\n{reply.text}", reply.buttons)
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
        conv.context["nlu_failures"] = 0
        if "lang" not in conv.context:
            return self._lang_screen(conv)
        return self._greeting_with_menu(session, conv)

    def _lang_screen(self, conv: Conversation) -> Reply:
        return Reply(t("choose_lang", self._lang(conv)),
                     (Button(t("btn_lang_uz", "ru"), "lang:uz"),
                      Button(t("btn_lang_ru", "ru"), "lang:ru")))

    def _greeting_with_menu(self, session: Session, conv: Conversation) -> Reply:
        lang = self._lang(conv)
        conv.context["greeting_shown"] = True
        greeting = t("greeting", lang, clinic=self._clinic_name(session))
        return Reply(f"{greeting}\n\n{t('menu_hint', lang)}", menu=menu_rows(lang))

    def _abort_pending(self, conv: Conversation) -> bool:
        """Меню/старт посреди сценария = явная смена намерения.

        Висящий hold отпускаем: бронь слота не должна переживать отказ
        от записи. Возвращает True, если живой hold действительно отменён
        (протухший/уже отменённый — нет: цель и так была достигнута).
        """
        cancelled = False
        appt = conv.context.get("appointment_id")
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
        conv.context["nlu_failures"] = 0
        lang = "ru" if extraction.language == "mixed" else extraction.language
        conv.context["lang"] = lang

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
                    and not conv.context.get("service") \
                    and self._service_id(session, "checkup") is not None:
                # наличие спрашивают без услуги — сетку считаем по осмотру
                conv.context["service"] = "checkup"
            return self._continue_booking(session, conv, extraction)
        if extraction.intent == "question":
            answer = self._answer_question(session, conv, extraction)
            return self._with_reprompt(session, conv, answer)
        if extraction.intent == "reschedule":
            return self._start_reschedule(session, conv, extraction)
        if extraction.intent == "cancel":
            return self._start_cancel(session, conv)
        return Reply(t("other_fallback", self._lang(conv)))

    def _on_nlu_failure(self, conv: Conversation) -> Reply:
        lang = self._lang(conv)
        failures = conv.context.get("nlu_failures", 0) + 1
        conv.context["nlu_failures"] = failures
        if failures >= MAX_NLU_FAILURES:
            self._notifier.notify(conv.chat_id, "2 кривых ответа NLU подряд",
                                  conv.context)
            conv.state = "escalated"
            return Reply(t("escalated", lang))
        return Reply(t("reask", lang))

    def _with_medical_disclaimer(self, conv: Conversation, extraction: Extraction,
                                 reply: Reply) -> Reply:
        if extraction.is_medical and not conv.context.get("medical_shown"):
            conv.context["medical_shown"] = True
            disclaimer = MEDICAL_DISCLAIMER[self._lang(conv)]
            return Reply(f"{disclaimer}\n\n{reply.text}", reply.buttons)
        return reply

    # ── Slot-filling записи ──────────────────────────────────────────────

    def _continue_booking(self, session: Session, conv: Conversation,
                          extraction: Extraction) -> Reply:
        ctx = conv.context
        if conv.state == "cancel_confirm":
            # пациент передумал отменять и начал новую запись
            ctx.pop("cancel_id", None)
            ctx.pop("cancel_when", None)
        if extraction.service:
            ctx["service"] = extraction.service
        self._merge_when(session, ctx, extraction)
        if extraction.doctor:
            self._resolve_doctor(session, ctx, extraction.doctor)
        return self._advance_booking(session, conv)

    def _merge_when(self, session: Session, ctx: dict, extraction: Extraction) -> None:
        if extraction.date_ref:
            resolved = resolve_date_ref(extraction.date_ref, self._today(session))
            ctx["date"] = resolved.isoformat()
        if extraction.time_ref:
            ctx["time_ref"] = extraction.time_ref

    def _advance_booking(self, session: Session, conv: Conversation) -> Reply:
        ctx = conv.context
        lang = self._lang(conv)
        if not ctx.get("service"):
            conv.state = "booking_collect"
            return Reply(t("ask_service", lang), self._service_buttons(session, lang))
        if not ctx.get("date"):
            conv.state = "booking_collect"
            return Reply(t("ask_date", lang), self._date_buttons(session, lang))
        return self._offer_slots(session, conv)

    def _offer_slots(self, session: Session, conv: Conversation,
                     note: str | None = None) -> Reply:
        ctx = conv.context
        lang = self._lang(conv)
        service_id = self._service_id(session, ctx["service"])
        if service_id is None:
            # клиника не оказывает услугу из NLU — спрашиваем из своего каталога
            ctx.pop("service", None)
            conv.state = "booking_collect"
            return Reply(t("ask_service", lang), self._service_buttons(session, lang))

        doctors = self._doctors(session, ctx.get("doctor_id"))
        tz = self._clinic_tz(session)
        asked = date.fromisoformat(ctx["date"])
        day, slots = self._collect_slots(session, doctors, service_id, asked,
                                         ctx.get("time_ref"))
        if not slots:
            self._notifier.notify(conv.chat_id, "нет слотов на 2 недели вперёд", ctx)
            self._clear_booking(conv)
            conv.state = "idle"
            return Reply(t("no_slots_at_all", lang))

        multi_doctor = len(doctors) > 1
        buttons = [
            Button(self._slot_label(start, doctor_name, tz, multi_doctor),
                   f"slot:{doctor_id}:{start.isoformat()}")
            for start, doctor_id, doctor_name in slots[:SLOTS_PER_REPLY]
        ]
        buttons.append(Button(t("btn_other_time", lang), "ask_date"))

        prefix = ""
        if ctx.pop("doctor_miss", None):
            prefix = t("doctor_not_found", lang) + "\n"
        if note:
            prefix = t(note, lang) + "\n" + prefix
        ctx["date"] = day.isoformat()
        conv.state = "booking_offer_slots"
        return Reply(prefix + self._offer_body(session, lang, asked, day),
                     tuple(buttons))

    def _collect_slots(self, session: Session, doctors, service_id,
                       asked: date, time_ref: str | None):
        """Первый день (от asked, до 2 недель вперёд) с подходящими слотами."""
        tz = self._clinic_tz(session)
        start_day = max(asked, self._today(session))
        now_utc = self._clock()
        for offset in range(NEAREST_DAY_SCAN + 1):
            day = start_day + timedelta(days=offset)
            slots = [
                (slot.start, doctor_id, doctor_name)
                for doctor_id, doctor_name in doctors
                for slot in self._sched.find_free_slots(doctor_id, service_id, day)
                if slot.start > now_utc  # сегодняшние прошедшие слоты не предлагаем
                and matches_time_ref(time_ref, slot.start.astimezone(tz).time())
            ]
            if slots:
                slots.sort(key=lambda s: (s[0], str(s[1])))
                return day, slots
        return start_day, []

    def _offer_body(self, session: Session, lang: str, asked: date, day: date) -> str:
        today = self._today(session)
        if max(asked, today) == today and self._closed_now(session):
            # пациент метит в «сегодня», а клиника вне рабочего окна: говорим
            # прямо «закрыто» — иначе ответ читается как «всё занято» (P0 BRIEF)
            return t("closed_now_slots", lang, date=f"{day:%d.%m}")
        if day == asked:
            return t("offer_slots", lang, date=f"{day:%d.%m}")
        return t("offer_slots_other_day", lang, asked=f"{asked:%d.%m}",
                 date=f"{day:%d.%m}")

    def _closed_now(self, session: Session) -> bool:
        """Клиника вне рабочего окна прямо сейчас (выходной/праздник — тоже)."""
        today = self._today(session)
        schedules = session.execute(
            text("SELECT working_intervals FROM doctor")).scalars().all()
        holidays = set(session.execute(
            text("SELECT date FROM holiday WHERE date = :day"), {"day": today}
        ).scalars())
        bounds = open_bounds(schedules, today, self._clinic_tz(session), holidays)
        if bounds is None:
            return True
        lo, hi = bounds
        return not (lo <= self._clock() < hi)

    # ── Кнопки (callback-actions) ────────────────────────────────────────

    def _process_action(self, session: Session, conv: Conversation, action: str) -> Reply:
        lang = self._lang(conv)
        if conv.state == "escalated":
            return Reply(t("escalated", lang))
        kind, _, rest = action.partition(":")
        if kind == "lang":
            conv.context["lang"] = rest
            if not conv.context.get("greeting_shown"):
                return self._greeting_with_menu(session, conv)
            # посреди сценария _with_reprompt отдаёт inline-кнопки шага, и menu
            # из note теряется — reply_markup в Telegram один. Reply-клавиатура
            # перерисуется на новом языке со следующим menu-сообщением.
            note = Reply(t("lang_changed", rest), menu=menu_rows(rest))
            return self._with_reprompt(session, conv, note)
        if kind == "service":
            conv.context["service"] = rest
            return self._advance_booking(session, conv)
        if kind == "date":
            conv.context["date"] = rest
            if conv.context.get("resched_id"):
                return self._offer_resched_slots(session, conv)
            return self._advance_booking(session, conv)
        if kind == "ask_date":
            if not conv.context.get("resched_id"):
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
        return Reply(t("other_fallback", lang))

    def _on_slot_chosen(self, session: Session, conv: Conversation,
                        doctor_id: str, start_iso: str) -> Reply:
        ctx = conv.context
        lang = self._lang(conv)
        service_id = self._service_id(session, ctx["service"])
        start = datetime.fromisoformat(start_iso)
        patient = find_patient_by_chat(session, conv.chat_id)
        try:
            appointment_id = self._sched.hold(
                uuid.UUID(doctor_id), service_id, start,
                patient_id=patient.id if patient else None,
                tg_chat_id=conv.chat_id,
            )
        except (SlotTakenError, InvalidSlotError):
            return self._offer_slots(session, conv, note="slot_taken")

        ctx["slot_start"] = start_iso
        ctx["slot_doctor"] = dict(self._doctors(session)).get(uuid.UUID(doctor_id))
        if patient is None:
            ctx["appointment_id"] = str(appointment_id)
            conv.state = "awaiting_name"
            return Reply(t("ask_name", lang))
        return self._confirm_and_finish(session, conv, appointment_id)

    # ── Имя и телефон нового пациента ────────────────────────────────────

    def _on_name(self, session: Session, conv: Conversation, message: str) -> Reply:
        # PII: имя не должно уходить в LLM — NLU дёргаем только для
        # вопросоподобного текста (прерывание вбок)
        if _looks_like_question(message):
            extraction = self._try_extract(message)
            if extraction is not None and extraction.intent == "question":
                answer = self._answer_question(session, conv, extraction)
                return Reply(f"{answer.text}\n\n{t('ask_name', self._lang(conv))}")
        conv.context["pending_name"] = message.strip()
        conv.state = "awaiting_phone"
        lang = self._lang(conv)
        return Reply(t("ask_phone", lang),
                     contact_request=t("btn_share_contact", lang))

    def _on_phone(self, session: Session, conv: Conversation, message: str) -> Reply:
        """Текст на шаге телефона: ручной ввод не принимается — только кнопка.

        PII: телефон текстом в NLU не уходит — NLU дёргается только для
        вопросоподобного текста (прерывание вбок), как на шаге имени.
        """
        lang = self._lang(conv)
        if _looks_like_question(message):
            extraction = self._try_extract(message)
            if extraction is not None and extraction.intent == "question":
                answer = self._answer_question(session, conv, extraction)
                return Reply(f"{answer.text}\n\n{t('ask_phone', lang)}",
                             contact_request=t("btn_share_contact", lang))
        return Reply(t("press_contact_button", lang),
                     contact_request=t("btn_share_contact", lang))

    def _process_contact(self, session: Session, conv: Conversation,
                         phone: str, own: bool) -> Reply:
        lang = self._lang(conv)
        if conv.state == "escalated":
            return Reply(t("escalated", lang))
        if conv.state != "awaiting_phone":
            return Reply(t("other_fallback", lang))
        if not own:
            return Reply(t("foreign_contact", lang),
                         contact_request=t("btn_share_contact", lang))
        try:
            phone = normalize_phone(phone)
        except ValueError:
            # свой контакт с не-узбекским номером: ручного ввода нет — тупик,
            # лид передаётся живому администратору
            self._notifier.notify(conv.chat_id,
                                  "контакт с не-узбекским номером", conv.context)
            conv.state = "escalated"
            return Reply(t("escalated", lang))

        patient_id = create_patient(session, conv.chat_id,
                                    conv.context["pending_name"], phone)
        conv.patient_id = str(patient_id)
        appointment_id = uuid.UUID(conv.context["appointment_id"])
        reply = self._confirm_and_finish(session, conv, appointment_id)
        if conv.state == "idle":
            # привязка пациента к записи — после confirm: его транзакция
            # обновляет ту же строку, держать её под нашим локом нельзя
            session.execute(
                text("UPDATE appointment SET patient_id = :p WHERE id = :a"),
                {"p": patient_id, "a": appointment_id},
            )
            # запись подтверждена — контакт-клавиатуру заменяет главное меню
            reply = replace(reply, menu=menu_rows(lang))
        return reply

    def _confirm_and_finish(self, session: Session, conv: Conversation,
                            appointment_id: uuid.UUID) -> Reply:
        ctx = conv.context
        lang = self._lang(conv)
        if self._slot_guard is not None and not self._guard_allows(session,
                                                                   appointment_id):
            # календарь уже занят (sync ещё не довёз) — слот не наш
            self._sched.cancel(appointment_id)
            ctx.pop("appointment_id", None)
            return self._offer_slots(session, conv, note="slot_taken")
        try:
            self._sched.confirm(appointment_id)
        except HoldExpiredError:
            ctx.pop("appointment_id", None)
            return self._offer_slots(session, conv, note="hold_expired")
        local = datetime.fromisoformat(ctx["slot_start"]).astimezone(self._clinic_tz(session))
        doctor = f", {ctx['slot_doctor']}" if ctx.get("slot_doctor") else ""
        reply = Reply(t("booked", lang, service=service_label(ctx["service"], lang),
                        when=f"{local:%d.%m %H:%M}", doctor=doctor))
        self._clear_booking(conv)
        conv.state = "idle"
        return reply

    # ── Перенос ──────────────────────────────────────────────────────────

    def _start_reschedule(self, session: Session, conv: Conversation,
                          extraction: Extraction) -> Reply:
        ctx = conv.context
        lang = self._lang(conv)
        appointment = self._find_active_appointment(session, conv.chat_id)
        if appointment is None:
            self._clear_booking(conv)
            conv.state = "idle"
            return Reply(t("resched_none", lang))
        ctx["resched_id"] = str(appointment.id)
        ctx["resched_doctor"] = str(appointment.doctor_id)
        # услугу не переспрашиваем — переносим ту же; перенос остаётся
        # у того же врача (engine.reschedule врача не меняет)
        ctx["service"] = self._service_name(session, appointment.service_id) or "checkup"
        self._merge_when(session, ctx, extraction)
        conv.state = "resched_offer_slots"
        if not ctx.get("date"):
            return Reply(t("ask_date", lang), self._date_buttons(session, lang))
        return self._offer_resched_slots(session, conv)

    def _offer_resched_slots(self, session: Session, conv: Conversation,
                             note: str | None = None) -> Reply:
        ctx = conv.context
        lang = self._lang(conv)
        service_id = self._service_id(session, ctx["service"])
        doctors = self._doctors(session, ctx["resched_doctor"])
        tz = self._clinic_tz(session)
        asked = date.fromisoformat(ctx["date"])
        day, slots = self._collect_slots(session, doctors, service_id, asked,
                                         ctx.get("time_ref"))
        if not slots:
            self._notifier.notify(conv.chat_id,
                                  "перенос: нет слотов на 2 недели вперёд", ctx)
            self._clear_booking(conv)
            conv.state = "idle"
            return Reply(t("no_slots_at_all", lang))
        buttons = [
            Button(self._slot_label(start, doctor_name, tz, multi_doctor=False),
                   f"reslot:{start.isoformat()}")
            for start, _doctor_id, doctor_name in slots[:SLOTS_PER_REPLY]
        ]
        buttons.append(Button(t("btn_other_time", lang), "ask_date"))
        prefix = t(note, lang) + "\n" if note else ""
        ctx["date"] = day.isoformat()
        conv.state = "resched_offer_slots"
        return Reply(prefix + self._offer_body(session, lang, asked, day),
                     tuple(buttons))

    def _on_reslot(self, session: Session, conv: Conversation, start_iso: str) -> Reply:
        ctx = conv.context
        lang = self._lang(conv)
        try:
            self._sched.reschedule(uuid.UUID(ctx["resched_id"]),
                                   datetime.fromisoformat(start_iso))
        except (SlotTakenError, InvalidSlotError):
            return self._offer_resched_slots(session, conv, note="slot_taken")
        except AppointmentNotFoundError:
            self._clear_booking(conv)
            conv.state = "idle"
            return Reply(t("resched_none", lang))
        local = datetime.fromisoformat(start_iso).astimezone(self._clinic_tz(session))
        self._clear_booking(conv)
        conv.state = "idle"
        return Reply(t("resched_done", lang, when=f"{local:%d.%m %H:%M}"))

    # ── Отмена ───────────────────────────────────────────────────────────

    def _start_cancel(self, session: Session, conv: Conversation) -> Reply:
        return self._begin_cancel(
            session, conv, self._find_active_appointment(session, conv.chat_id))

    def _start_cancel_by_id(self, session: Session, conv: Conversation,
                            appointment_id: str) -> Reply:
        """Кнопка «Отменить» из напоминания: запись известна по id."""
        appointment = session.execute(
            text("SELECT id, lower(time_range) AS start FROM appointment "
                 "WHERE id = CAST(:id AS uuid) AND status IN ('hold', 'booked')"),
            {"id": appointment_id},
        ).one_or_none()
        reply = self._begin_cancel(session, conv, appointment)
        if conv.context.get("cancel_id"):
            # источник отмены — напоминание: метрика предотвращённых неявок
            conv.context["cancel_via"] = "reminder"
        return reply

    def _begin_cancel(self, session: Session, conv: Conversation,
                      appointment) -> Reply:
        ctx = conv.context
        lang = self._lang(conv)
        if appointment is None:
            self._clear_booking(conv)
            conv.state = "idle"
            return Reply(t("cancel_none", lang))
        local = appointment.start.astimezone(self._clinic_tz(session))
        ctx["cancel_id"] = str(appointment.id)
        ctx["cancel_when"] = f"{local:%d.%m %H:%M}"
        conv.state = "cancel_confirm"
        return self._cancel_prompt(conv)

    def _cancel_prompt(self, conv: Conversation) -> Reply:
        lang = self._lang(conv)
        return Reply(
            t("cancel_confirm_q", lang, when=conv.context["cancel_when"]),
            (Button(t("btn_yes", lang), "cancel_yes"),
             Button(t("btn_no", lang), "cancel_no")),
        )

    def _on_cancel_confirmed(self, session: Session, conv: Conversation) -> Reply:
        lang = self._lang(conv)
        cancel_id = conv.context.get("cancel_id")
        cancel_via = conv.context.get("cancel_via")  # до _clear_booking
        self._clear_booking(conv)
        conv.state = "idle"
        try:
            self._sched.cancel(uuid.UUID(cancel_id), actor=cancel_via)
        except AppointmentNotFoundError:
            return Reply(t("cancel_none", lang))
        return Reply(t("cancel_done", lang))

    # ── Вопросы ──────────────────────────────────────────────────────────

    def _price_list(self, session: Session, conv: Conversation) -> Reply:
        """Весь прайс из каталога services; пустой каталог — к администратору."""
        lang = self._lang(conv)
        rows = session.execute(
            text("SELECT name, price FROM service ORDER BY name")).all()
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
            price = session.execute(
                text("SELECT price FROM service WHERE name = :name LIMIT 1"),
                {"name": extraction.service},
            ).scalar_one_or_none()
            if price is None:
                return Reply(t("price_unknown", lang, service=label))
            formatted = f"{int(price):,}".replace(",", " ")
            return Reply(t("price_answer", lang, service=label, price=formatted))
        self._notifier.notify(conv.chat_id, "вопрос вне компетенции бота", conv.context)
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

    # ── Вспомогательное ──────────────────────────────────────────────────

    def _try_extract(self, message: str) -> Extraction | None:
        try:
            return self._extractor.extract(message)
        except ExtractionError:
            return None

    def _lang(self, conv: Conversation) -> str:
        return conv.context.get("lang", "ru")

    def _clear_booking(self, conv: Conversation) -> None:
        for key in _BOOKING_KEYS:
            conv.context.pop(key, None)

    def _clinic_name(self, session: Session) -> str:
        return session.execute(
            text("SELECT name FROM clinic "
                 "WHERE id = current_setting('app.clinic_id')::uuid")
        ).scalar_one()

    def _clinic_tz(self, session: Session) -> ZoneInfo:
        if self._tz is None:
            self._tz = ZoneInfo(session.execute(
                text("SELECT timezone FROM clinic "
                     "WHERE id = current_setting('app.clinic_id')::uuid")
            ).scalar_one())
        return self._tz

    def _today(self, session: Session) -> date:
        return self._clock().astimezone(self._clinic_tz(session)).date()

    def _guard_allows(self, session: Session, appointment_id: uuid.UUID) -> bool:
        row = session.execute(
            text("SELECT doctor_id, lower(time_range) AS start, "
                 "upper(time_range) AS finish FROM appointment WHERE id = :id"),
            {"id": appointment_id},
        ).one()
        return self._slot_guard.is_free(row.doctor_id, row.start, row.finish)

    def _find_active_appointment(self, session: Session, chat_id: int):
        """Ближайшая будущая активная запись чата (для переноса/отмены)."""
        return session.execute(
            text("SELECT id, doctor_id, service_id, lower(time_range) AS start "
                 "FROM appointment "
                 "WHERE tg_chat_id = :chat AND status IN ('hold', 'booked') "
                 "AND lower(time_range) > now() "
                 "ORDER BY lower(time_range) LIMIT 1"),
            {"chat": chat_id},
        ).one_or_none()

    def _service_name(self, session: Session, service_id) -> str | None:
        if service_id is None:
            return None
        return session.execute(
            text("SELECT name FROM service WHERE id = :id"), {"id": service_id}
        ).scalar_one_or_none()

    def _service_id(self, session: Session, service_key: str) -> uuid.UUID | None:
        return session.execute(
            text("SELECT id FROM service WHERE name = :name ORDER BY name LIMIT 1"),
            {"name": service_key},
        ).scalar_one_or_none()

    def _service_buttons(self, session: Session, lang: str) -> tuple[Button, ...]:
        names = session.execute(
            text("SELECT name FROM service ORDER BY name")
        ).scalars().all()
        return tuple(Button(service_label(n, lang), f"service:{n}") for n in names)

    def _date_buttons(self, session: Session, lang: str) -> tuple[Button, ...]:
        today = self._today(session)
        return (
            Button(t("btn_today", lang), f"date:{today.isoformat()}"),
            Button(t("btn_tomorrow", lang), f"date:{(today + timedelta(days=1)).isoformat()}"),
            Button(t("btn_after_tomorrow", lang),
                   f"date:{(today + timedelta(days=2)).isoformat()}"),
        )

    def _doctors(self, session: Session,
                 only_id: str | None = None) -> list[tuple[uuid.UUID, str | None]]:
        rows = session.execute(
            text("SELECT id, name_encrypted FROM doctor ORDER BY id")
        ).all()
        doctors = [
            (row.id, decrypt_text(row.name_encrypted) if row.name_encrypted else None)
            for row in rows
        ]
        if only_id:
            doctors = [d for d in doctors if str(d[0]) == only_id]
        return doctors

    def _resolve_doctor(self, session: Session, ctx: dict, name: str) -> None:
        target = name.casefold()
        for doctor_id, doctor_name in self._doctors(session):
            if doctor_name and (target in doctor_name.casefold()
                                or doctor_name.casefold() in target):
                ctx["doctor_id"] = str(doctor_id)
                return
        ctx["doctor_miss"] = True

    def _slot_label(self, start: datetime, doctor_name: str | None,
                    tz: ZoneInfo, multi_doctor: bool) -> str:
        local = start.astimezone(tz)
        label = f"{local:%d.%m %H:%M}"
        if multi_doctor and doctor_name:
            label += f" · {doctor_name}"
        return label
