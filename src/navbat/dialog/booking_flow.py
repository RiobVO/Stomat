"""Сценарий записи (_BookingFlowMixin): slot-filling, выбор слота, сбор
имени/телефона нового пациента и финальное подтверждение. Вынесен из
god-object DialogEngine (R4); общие хелперы и роутер — через self."""
from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date, datetime

from sqlalchemy.orm import Session

from navbat.dialog import appointments_repo
from navbat.dialog.conversation import Conversation, DialogContext
from navbat.dialog.dates import resolve_date_ref
from navbat.dialog.dialog_common import SLOTS_PER_REPLY, _looks_like_question
from navbat.dialog.patients import create_patient_with_hash, find_patient_by_chat
from navbat.dialog.replies import Button, Reply, menu_rows, service_label, t
from navbat.nlu.schema import Extraction
from navbat.scheduling.errors import HoldExpiredError, InvalidSlotError, SlotTakenError


class _BookingFlowMixin:
    def _continue_booking(self, session: Session, conv: Conversation,
                          extraction: Extraction) -> Reply:
        ctx = conv.context
        if conv.state == "cancel_confirm":
            # пациент передумал отменять и начал новую запись
            ctx.cancel_id = None
            ctx.cancel_when = None
        if extraction.service:
            ctx.service = extraction.service
        self._merge_when(session, ctx, extraction)
        if extraction.doctor:
            self._resolve_doctor(session, ctx, extraction.doctor)
        return self._advance_booking(session, conv)

    def _merge_when(self, session: Session, ctx: DialogContext,
                    extraction: Extraction) -> None:
        if extraction.date_ref:
            resolved = resolve_date_ref(extraction.date_ref, self._today(session))
            ctx.date = resolved.isoformat()
        if extraction.time_ref:
            ctx.time_ref = extraction.time_ref

    def _advance_booking(self, session: Session, conv: Conversation) -> Reply:
        ctx = conv.context
        lang = self._lang(conv)
        if not ctx.service:
            conv.state = "booking_collect"
            return Reply(t("ask_service", lang), self._service_buttons(session, lang))
        if not ctx.date:
            conv.state = "booking_collect"
            return Reply(t("ask_date", lang), self._date_buttons(session, lang))
        return self._offer_slots(session, conv)

    def _offer_slots(self, session: Session, conv: Conversation,
                     note: str | None = None) -> Reply:
        ctx = conv.context
        lang = self._lang(conv)
        service_id = self._service_id(session, ctx.service)
        if service_id is None:
            # клиника не оказывает услугу из NLU — спрашиваем из своего каталога
            ctx.service = None
            conv.state = "booking_collect"
            return Reply(t("ask_service", lang), self._service_buttons(session, lang))

        doctors = self._doctors(session, ctx.doctor_id)
        tz = self._clinic_tz(session)
        asked = date.fromisoformat(ctx.date)
        day, slots = self._collect_slots(session, doctors, service_id, asked,
                                         ctx.time_ref)
        if not slots:
            self._notifier.notify(conv.chat_id, "нет слотов на 2 недели вперёд",
                                  self._escalation_context(conv))
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
        if ctx.doctor_miss:
            ctx.doctor_miss = False
            prefix = t("doctor_not_found", lang) + "\n"
        if note:
            prefix = t(note, lang) + "\n" + prefix
        ctx.date = day.isoformat()
        conv.state = "booking_offer_slots"
        return Reply(prefix + self._offer_body(session, lang, asked, day),
                     tuple(buttons))

    def _on_slot_chosen(self, session: Session, conv: Conversation,
                        doctor_id: str, start_iso: str) -> Reply:
        ctx = conv.context
        lang = self._lang(conv)
        service_id = self._service_id(session, ctx.service)
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

        ctx.slot_start = start_iso
        ctx.slot_doctor = dict(self._doctors(session)).get(uuid.UUID(doctor_id))
        if patient is None:
            ctx.appointment_id = str(appointment_id)
            conv.state = "awaiting_name"
            return Reply(t("ask_name", lang))
        return self._confirm_and_finish(session, conv, appointment_id)

    def _on_name(self, session: Session, conv: Conversation, message: str) -> Reply:
        # PII: имя не должно уходить в LLM — NLU дёргаем только для
        # вопросоподобного текста (прерывание вбок)
        if _looks_like_question(message):
            extraction = self._try_extract(message)
            if extraction is not None and extraction.intent == "question":
                answer = self._answer_question(session, conv, extraction)
                return Reply(f"{answer.text}\n\n{t('ask_name', self._lang(conv))}")
        conv.context.pending_name = message.strip()
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
                         phone_hash: str | None, own: bool) -> Reply:
        lang = self._lang(conv)
        if conv.state == "escalated":
            return Reply(t("escalated", lang))
        if conv.state != "awaiting_phone":
            return Reply(t("other_fallback", lang))
        if not own:
            return Reply(t("foreign_contact", lang),
                         contact_request=t("btn_share_contact", lang))
        if phone_hash is None:
            # свой контакт с не-узбекским номером: ручного ввода нет — тупик,
            # лид передаётся живому администратору
            self._notifier.notify(conv.chat_id,
                                  "контакт с не-узбекским номером",
                                  self._escalation_context(conv))
            conv.state = "escalated"
            return Reply(t("escalated", lang))

        patient_id = create_patient_with_hash(session, conv.chat_id,
                                              conv.context.pending_name, phone_hash)
        conv.patient_id = str(patient_id)
        appointment_id = uuid.UUID(conv.context.appointment_id)
        reply = self._confirm_and_finish(session, conv, appointment_id)
        if conv.state == "idle":
            # привязка пациента к записи — после confirm: его транзакция
            # обновляет ту же строку, держать её под нашим локом нельзя
            appointments_repo.set_patient(session, appointment_id, patient_id)
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
            ctx.appointment_id = None
            return self._offer_slots(session, conv, note="slot_taken")
        try:
            self._sched.confirm(appointment_id)
        except HoldExpiredError:
            ctx.appointment_id = None
            return self._offer_slots(session, conv, note="hold_expired")
        local = datetime.fromisoformat(ctx.slot_start).astimezone(self._clinic_tz(session))
        doctor = f", {ctx.slot_doctor}" if ctx.slot_doctor else ""
        reply = Reply(t("booked", lang, service=service_label(ctx.service, lang),
                        when=f"{local:%d.%m %H:%M}", doctor=doctor))
        self._clear_booking(conv)
        conv.state = "idle"
        return reply
