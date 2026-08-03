"""Сценарий переноса (_RescheduleFlowMixin): найти активную запись,
предложить новые слоты у того же врача, выполнить reschedule. Вынесен из
god-object DialogEngine (R4); общие хелперы — через self."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy.orm import Session

from navbat.dialog import appointments_repo, clinic_repo, doctors_repo
from navbat.dialog.conversation import Conversation
from navbat.dialog.dialog_common import SLOTS_PER_REPLY
from navbat.dialog.escalation import booking_feed
from navbat.dialog.replies import Button, Reply, card_lines, service_label, t
from navbat.nlu.schema import Extraction
from navbat.telegram.admin_texts import Reason
from navbat.scheduling.errors import (
    AppointmentNotFoundError,
    InvalidSlotError,
    SlotTakenError,
)


def reslot_action(appointment_id, start: datetime) -> str:
    """callback кнопки альтернативного слота: `reslot:<id записи>:<минуты эпохи>`.

    Кнопка живёт в чате до самого приёма (её шлёт и календарный синк при
    вытеснении записи), поэтому уходит сырой — мимо tg_actions-map. Раз так,
    она обязана нести СВОЮ запись: контекст диалога один на чат и
    перезаписывается следующим сценарием, а второе вытеснение в том же
    проходе синка переставляло resched_id на другую запись того же пациента.
    В callback_data 64 байта, поэтому время — минуты эпохи (как у wl:take):
    7 + 36 + 1 + 8 = 52 байта.
    """
    return f"reslot:{appointment_id}:{int(start.timestamp()) // 60}"


class _RescheduleFlowMixin:
    def _start_reschedule(self, session: Session, conv: Conversation,
                          extraction: Extraction) -> Reply:
        ctx = conv.context
        lang = self._lang(conv)
        appointment = self._find_active_appointment(session, conv.chat_id)
        if appointment is None:
            self._clear_booking(conv)
            conv.state = "idle"
            return Reply(t("resched_none", lang))
        # перенос — это уже не продолжение приглашения: пациент занялся
        # существующей записью, и конверсию recall засчитывать нечему
        ctx.recall_source = None
        ctx.resched_id = str(appointment.id)
        ctx.resched_doctor = str(appointment.doctor_id)
        # услугу не переспрашиваем — переносим ту же; перенос остаётся
        # у того же врача (engine.reschedule врача не меняет)
        ctx.service = self._service_name(session, appointment.service_id) or "checkup"
        self._merge_when(session, ctx, extraction)
        conv.state = "resched_offer_slots"
        if not ctx.date:
            # единый ответ выбора дня (с календарём); resched-state
            # сохраняется — _ask_date его не трогает при resched_id
            return self._ask_date(session, conv)
        return self._offer_resched_slots(session, conv)

    def _offer_resched_slots(self, session: Session, conv: Conversation,
                             note: str | None = None) -> Reply:
        ctx = conv.context
        lang = self._lang(conv)
        service_id = self._service_id(session, ctx.service)
        doctors = self._doctors(session, ctx.resched_doctor)
        tz = self._clinic_tz(session)
        asked = date.fromisoformat(ctx.date)
        day, slots = self._collect_slots(session, doctors, service_id, asked,
                                         ctx.time_ref)
        if not slots:
            # П-5: календарь вместо «передаю администратору» (FYI раз в день)
            return self._no_slots_calendar(session, conv,
                                           Reason("reason_no_slots_reschedule"))
        buttons = [
            Button(self._slot_label(start, doctor_name, tz, multi_doctor=False),
                   reslot_action(ctx.resched_id, start))
            for start, _doctor_id, doctor_name in slots[:SLOTS_PER_REPLY]
        ]
        buttons.append(Button(t("btn_other_time", lang), "ask_date"))
        prefix = t(note, lang) + "\n" if note else ""
        ctx.date = day.isoformat()
        conv.state = "resched_offer_slots"
        return Reply(prefix + self._offer_body(session, lang, asked, day,
                                               ctx.time_ref),
                     tuple(buttons))

    def _on_reslot(self, session: Session, conv: Conversation, rest: str) -> Reply:
        """Тап по альтернативному слоту: `reslot:<id записи>:<минуты эпохи>`.

        Всё, что пришло, — данные от клиента: и запись, и время. Запись
        сверяется с чатом (чужой id внутри клиники двигал бы чужую запись),
        время — с текущим моментом: движок сверяет старт только с рабочей
        сеткой врача, поэтому валидное прошлое время на сетке уносило запись
        в прошлое. Кнопка висит в чате до самого приёма — успевает и то и то.
        """
        ctx = conv.context
        lang = self._lang(conv)

        def stale() -> Reply:
            return self._with_reprompt(session, conv,
                                       Reply(t("stale_button", lang)))

        appointment_raw, _, minutes_raw = rest.partition(":")
        if not minutes_raw.isdigit():
            return stale()
        try:
            uuid.UUID(appointment_raw)
        except ValueError:
            return stale()
        appointment = appointments_repo.active_by_id(session, appointment_raw,
                                                     conv.chat_id)
        if appointment is None:
            self._clear_booking(conv)
            conv.state = "idle"
            return Reply(t("resched_none", lang))
        tz = self._clinic_tz(session)
        try:
            new_start = datetime.fromtimestamp(int(minutes_raw) * 60, tz=tz)
        except (OverflowError, OSError, ValueError):
            return stale()
        if new_start <= datetime.now(tz):
            return stale()
        # контекст переезжает на ту запись, которую назвала кнопка: иначе
        # переспрос «слот занят» предложит слоты под чужую запись
        ctx.recall_source = None  # перенос — не продолжение приглашения
        ctx.resched_id = str(appointment.id)
        ctx.resched_doctor = str(appointment.doctor_id)
        ctx.service = self._service_name(session, appointment.service_id) \
            or ctx.service
        ctx.date = new_start.date().isoformat()
        conv.state = "resched_offer_slots"
        try:
            self._sched.reschedule(appointment.id, new_start)
        except (SlotTakenError, InvalidSlotError):
            return self._offer_resched_slots(session, conv, note="slot_taken")
        except AppointmentNotFoundError:
            self._clear_booking(conv)
            conv.state = "idle"
            return Reply(t("resched_none", lang))
        local = new_start.astimezone(tz)
        # карточка-«билет», как booked: услуга/врач/адрес — до _clear_booking,
        # он стирает контекст
        service = service_label(ctx.service or "checkup", lang)
        lines = card_lines(doctors_repo.doctor_name(session, appointment.doctor_id),
                           clinic_repo.clinic_address(session))
        self._clear_booking(conv)
        conv.state = "idle"
        # единственная точка переноса пациентом: и сценарий переноса, и
        # альтернативы вытесненной записи приходят сюда одной кнопкой reslot:
        booking_feed(self._notifier, session, appointment.id, "resched")
        return Reply(t("resched_done", lang, service=service,
                       when=f"{local:%d.%m %H:%M}", **lines))
