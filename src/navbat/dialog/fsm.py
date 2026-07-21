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
from datetime import date, datetime, timedelta, timezone
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
    Button,
    Reply,
    service_label,
    t,
)
from navbat.nlu.extractor import ExtractionError, Extractor
from navbat.nlu.schema import Extraction
from navbat.scheduling.engine import SchedulingEngine
from navbat.scheduling.errors import (
    HoldExpiredError,
    InvalidSlotError,
    SlotTakenError,
)

MAX_NLU_FAILURES = 2     # подряд; дальше — эскалация
SLOTS_PER_REPLY = 4      # кнопок со временем в одном ответе
NEAREST_DAY_SCAN = 14    # дней вперёд при поиске свободного дня

# ключи контекста, относящиеся к текущей записи (чистятся по завершении)
_BOOKING_KEYS = ("service", "date", "time_ref", "doctor_id", "doctor_miss",
                 "appointment_id", "slot_start", "slot_doctor", "pending_name")


class DialogEngine:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clinic_id: uuid.UUID,
        extractor: Extractor,
        notifier: EscalationNotifier | None = None,
        scheduler: SchedulingEngine | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clinic_id = clinic_id
        self._extractor = extractor
        self._notifier = notifier or LoggingEscalation()
        self._sched = scheduler or SchedulingEngine(session_factory, clinic_id)
        self._tz: ZoneInfo | None = None

    # ── Входные точки ────────────────────────────────────────────────────

    def handle_text(self, chat_id: int, message: str) -> Reply:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            conv = load_conversation(session, chat_id)
            reply = self._process_text(session, conv, message)
            save_conversation(session, conv)
        return reply

    def handle_action(self, chat_id: int, action: str) -> Reply:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            conv = load_conversation(session, chat_id)
            reply = self._process_action(session, conv, action)
            save_conversation(session, conv)
        return reply

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
        if extraction.intent == "book" or (
            extraction.intent == "question"
            and (extraction.date_ref or extraction.time_ref)
        ):
            return self._continue_booking(session, conv, extraction)
        if extraction.intent == "question":
            return self._answer_question(session, conv, extraction)
        # reschedule/cancel и прерывания — следующий шаг инкремента
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
        if extraction.service:
            ctx["service"] = extraction.service
        if extraction.date_ref:
            resolved = resolve_date_ref(extraction.date_ref, self._today(session))
            ctx["date"] = resolved.isoformat()
        if extraction.time_ref:
            ctx["time_ref"] = extraction.time_ref
        if extraction.doctor:
            self._resolve_doctor(session, ctx, extraction.doctor)
        return self._advance_booking(session, conv)

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
        today = self._today(session)
        start_day = max(asked, today)
        now_utc = datetime.now(timezone.utc)

        day, slots = start_day, []
        for offset in range(NEAREST_DAY_SCAN + 1):
            day = start_day + timedelta(days=offset)
            slots = [
                (slot.start, doctor_id, doctor_name)
                for doctor_id, doctor_name in doctors
                for slot in self._sched.find_free_slots(doctor_id, service_id, day)
                if slot.start > now_utc  # сегодняшние прошедшие слоты не предлагаем
                and matches_time_ref(ctx.get("time_ref"),
                                     slot.start.astimezone(tz).time())
            ]
            if slots:
                break
        else:
            self._notifier.notify(conv.chat_id, "нет слотов на 2 недели вперёд", ctx)
            self._clear_booking(conv)
            return Reply(t("no_slots_at_all", lang))

        slots.sort(key=lambda s: (s[0], str(s[1])))
        multi_doctor = len(doctors) > 1
        buttons = [
            Button(self._slot_label(start, doctor_name, tz, multi_doctor),
                   f"slot:{doctor_id}:{start.isoformat()}")
            for start, doctor_id, doctor_name in slots[:SLOTS_PER_REPLY]
        ]
        buttons.append(Button(t("btn_other_time", lang), "ask_date"))

        day_str = f"{day:%d.%m}"
        if day == asked:
            body = t("offer_slots", lang, date=day_str)
        else:
            body = t("offer_slots_other_day", lang, asked=f"{asked:%d.%m}", date=day_str)
        prefix = ""
        if ctx.pop("doctor_miss", None):
            prefix = t("doctor_not_found", lang) + "\n"
        if note:
            prefix = t(note, lang) + "\n" + prefix
        ctx["date"] = day.isoformat()
        conv.state = "booking_offer_slots"
        return Reply(prefix + body, tuple(buttons))

    # ── Кнопки (callback-actions) ────────────────────────────────────────

    def _process_action(self, session: Session, conv: Conversation, action: str) -> Reply:
        lang = self._lang(conv)
        if conv.state == "escalated":
            return Reply(t("escalated", lang))
        kind, _, rest = action.partition(":")
        if kind == "service":
            conv.context["service"] = rest
            return self._advance_booking(session, conv)
        if kind == "date":
            conv.context["date"] = rest
            return self._advance_booking(session, conv)
        if kind == "ask_date":
            conv.state = "booking_collect"
            return Reply(t("ask_date", lang), self._date_buttons(session, lang))
        if kind == "slot":
            doctor_id, _, start_iso = rest.partition(":")
            return self._on_slot_chosen(session, conv, doctor_id, start_iso)
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
        extraction = self._try_extract(message)
        if extraction is not None and extraction.intent == "question":
            answer = self._answer_question(session, conv, extraction)
            return Reply(f"{answer.text}\n\n{t('ask_name', self._lang(conv))}")
        # имя — свободный текст, NLU его не знает (и не должен)
        conv.context["pending_name"] = message.strip()
        conv.state = "awaiting_phone"
        return Reply(t("ask_phone", self._lang(conv)))

    def _on_phone(self, session: Session, conv: Conversation, message: str) -> Reply:
        lang = self._lang(conv)
        try:
            phone = normalize_phone(message)
        except ValueError:
            extraction = self._try_extract(message)
            if extraction is not None and extraction.intent == "question":
                answer = self._answer_question(session, conv, extraction)
                return Reply(f"{answer.text}\n\n{t('ask_phone', lang)}")
            return Reply(t("bad_phone", lang))

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
        return reply

    def _confirm_and_finish(self, session: Session, conv: Conversation,
                            appointment_id: uuid.UUID) -> Reply:
        ctx = conv.context
        lang = self._lang(conv)
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

    # ── Вопросы (минимум; полный FAQ — следующий шаг) ────────────────────

    def _answer_question(self, session: Session, conv: Conversation,
                         extraction: Extraction) -> Reply:
        return Reply(t("other_fallback", self._lang(conv)))

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

    def _clinic_tz(self, session: Session) -> ZoneInfo:
        if self._tz is None:
            self._tz = ZoneInfo(session.execute(
                text("SELECT timezone FROM clinic "
                     "WHERE id = current_setting('app.clinic_id')::uuid")
            ).scalar_one())
        return self._tz

    def _today(self, session: Session) -> date:
        return datetime.now(self._clinic_tz(session)).date()

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
