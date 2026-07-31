"""Общие хелперы диалога (_SharedHelpersMixin): поиск слотов, доступ к
клинике/врачам/услугам/датам, язык и очистка контекста. Вынесены из
god-object DialogEngine (R4); используются всеми сценариями через self —
DialogEngine наследует этот mixin вместе со сценарными."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from navbat.dialog import (
    appointments_repo,
    clinic_repo,
    doctors_repo,
    services_repo,
    waitlist_repo,
)
from navbat.dialog.conversation import Conversation, DialogContext
from navbat.dialog.dates import exact_time_ref, matches_time_ref
from navbat.dialog.dialog_common import NEAREST_DAY_SCAN
from navbat.dialog.replies import SERVICE_EMOJI, Button, Reply, service_label, t
from navbat.nlu.extractor import ExtractionError
from navbat.nlu.schema import Extraction
from navbat.scheduling.calendar_rules import open_bounds


class _SharedHelpersMixin:
    def _try_extract(self, message: str) -> Extraction | None:
        try:
            return self._extractor.extract(message)
        except ExtractionError:
            return None

    def _lang(self, conv: Conversation) -> str:
        return conv.context.lang or "ru"

    def _clear_booking(self, conv: Conversation) -> None:
        conv.context.clear_booking()

    def _escalation_context(self, conv: Conversation) -> dict:
        """Контекст для эскалации без PII пациента (имя) — он уходит
        в админ-чат и логи (m1)."""
        return conv.context.escalation_dict()

    def _escalated_reply(self, conv: Conversation,
                         key: str = "escalated") -> Reply:
        """Ответ замороженного диалога — всегда с выходом обратно к боту.

        Стоп-состояние остаётся стоп-состоянием (NLU не дёргается, повторных
        алертов админу нет), но пациент не заперт: тап делает то же, что
        /start, право на который у него уже есть (BRIEF разд. 14.A). Без
        этой кнопки reply-меню остаётся на экране мёртвым — живая проверка
        27.07.2026, карта продажи №5."""
        lang = self._lang(conv)
        return Reply(t(key, lang),
                     (Button(t("btn_back_to_bot", lang), "unfreeze"),))

    def _clinic_name(self, session: Session) -> str:
        return clinic_repo.clinic_name(session)

    def _clinic_tz(self, session: Session) -> ZoneInfo:
        if self._tz is None:
            self._tz = ZoneInfo(clinic_repo.clinic_timezone(session))
        return self._tz

    def _today(self, session: Session) -> date:
        return self._clock().astimezone(self._clinic_tz(session)).date()

    def _guard_allows(self, session: Session, appointment_id: uuid.UUID) -> bool:
        row = appointments_repo.slot_bounds(session, appointment_id)
        return self._slot_guard.is_free(row.doctor_id, row.start, row.finish)

    def _find_active_appointment(self, session: Session, chat_id: int):
        """Ближайшая будущая активная запись чата (для переноса/отмены)."""
        return appointments_repo.active_by_chat(session, chat_id)

    def _service_name(self, session: Session, service_id) -> str | None:
        if service_id is None:
            return None
        return services_repo.service_name(session, service_id)

    def _service_id(self, session: Session, service_key: str) -> uuid.UUID | None:
        return services_repo.service_id(session, service_key)

    def _on_waitlist(self, session: Session, conv: Conversation,
                     rest: str) -> Reply:
        """Лист ожидания: wl:join:<service_key> | wl:leave:<id> |
        wl:take:<id>:<минуты эпохи> (тап по предложенному слоту)."""
        lang = self._lang(conv)
        op, _, arg = rest.partition(":")
        if op == "leave":
            if arg.isdigit():
                waitlist_repo.mark_cancelled(session, int(arg))
            return Reply(t("waitlist_left", lang))
        if op == "take":
            return self._take_waitlist_slot(session, conv, arg)
        # join: услуга из action, иначе из контекста, иначе осмотр (бэкстоп)
        service_id = self._service_id(session, arg or conv.context.service
                                      or "checkup")
        if service_id is None:
            return Reply(t("not_understood", lang))
        patient_id = uuid.UUID(conv.patient_id) if conv.patient_id else None
        wid = waitlist_repo.add(session, service_id, conv.chat_id,
                                patient_id, lang)
        self._clear_booking(conv)
        conv.state = "idle"
        return Reply(t("waitlist_already" if wid is None else "waitlist_joined",
                       lang))

    def _take_waitlist_slot(self, session: Session, conv: Conversation,
                            arg: str) -> Reply:
        """Тап по слоту из предложения очереди: wl:take:<id>:<минуты эпохи>.

        Услуга берётся из строки очереди — она источник истины, а не диалог
        (пациент мог за это время начать оформлять другую запись). Врач
        подбирается здесь же: очередь обещает «любой ближайший слот по
        услуге», а в callback_data места на uuid врача нет.
        """
        lang = self._lang(conv)
        wid_raw, _, minutes_raw = arg.partition(":")
        if not wid_raw.isdigit() or not minutes_raw.isdigit():
            return self._with_reprompt(session, conv,
                                       Reply(t("stale_button", lang)))
        row = waitlist_repo.active_by_id(session, int(wid_raw))
        if row is None or row.tg_chat_id != conv.chat_id:
            # очередь уже закрыта (записался, отписался, истекла) либо чужая
            return self._with_reprompt(session, conv,
                                       Reply(t("stale_button", lang)))
        service_key = self._service_name(session, row.service_id) or "checkup"
        if waitlist_repo.has_future_active(session, conv.chat_id, row.service_id):
            # повторный тап по тому же предложению: врач подбирается здесь,
            # поэтому второй тап занял бы ДРУГОГО свободного врача на то же
            # время — две записи на один час. Снимает с очереди только
            # следующий цикл матчера, окно реальное
            waitlist_repo.mark_fulfilled(session, row.id)
            return Reply(t("waitlist_taken_already", lang,
                           service=service_label(service_key, lang)))
        try:
            start = datetime.fromtimestamp(int(minutes_raw) * 60,
                                           tz=self._clinic_tz(session))
        except (ValueError, OverflowError, OSError):
            # время из callback_data: мусор не должен ронять обработку
            return self._with_reprompt(session, conv,
                                       Reply(t("stale_button", lang)))
        conv.context.service = service_key
        doctor_id = self._free_doctor_at(session, row.service_id, start)
        if doctor_id is None:  # слот заняли, пока предложение висело
            return self._offer_slots(session, conv, note="slot_taken")
        return self._on_slot_chosen(session, conv, str(doctor_id),
                                    start.isoformat())

    def _free_doctor_at(self, session: Session, service_id,
                        start: datetime) -> uuid.UUID | None:
        free = self._free_doctors_at(session, service_id, start)
        return free[0][0] if free else None

    def _free_doctors_at(self, session: Session, service_id, start: datetime,
                         preferred: str | None = None
                         ) -> list[tuple[uuid.UUID, str | None]]:
        """Врачи, свободные РОВНО на это время, — самый занятый последним.

        preferred — врач, которого пациент назвал сам («к Акмалю»): выбор
        уже сделан, предлагать вместо него другого нельзя.

        Порядок — балансировка: первым идёт тот, у кого в этот день больше
        свободных слотов, поэтому «Любой» не сажает всех к одному врачу.
        Счётчик берётся из того же обхода, лишних запросов нет.
        """
        if start <= self._clock():
            # кнопки time:/d:/wl:take несут время и живут в чате сколько
            # угодно, а движок сверяет старт только с рабочей сеткой врача:
            # валидное «09:00 прошлого понедельника» на сетке лежит, и тап по
            # вчерашнему сообщению записывал пациента в прошлое
            return []
        day = start.astimezone(self._clinic_tz(session)).date()
        free: list[tuple[int, str, uuid.UUID, str | None]] = []
        for doctor_id, name in self._doctors(session, preferred):
            slots = self._sched.find_free_slots(doctor_id, service_id, day)
            if any(slot.start == start for slot in slots):
                free.append((-len(slots), str(doctor_id), doctor_id, name))
        free.sort()
        return [(doctor_id, name) for _, _, doctor_id, name in free]

    def _on_time_chosen(self, session: Session, conv: Conversation,
                        start_iso: str) -> Reply:
        """Тап по времени: врача выбирает пациент, но только если есть выбор.

        Время приходит от клиента и сетка могла устареть — свободных врачей
        считаем заново.
        """
        lang = self._lang(conv)
        service_id = self._service_id(session, conv.context.service)
        try:
            start = datetime.fromisoformat(start_iso)
        except ValueError:
            return self._with_reprompt(session, conv,
                                       Reply(t("stale_button", lang)))
        free = (self._free_doctors_at(session, service_id, start,
                                      conv.context.doctor_id)
                if service_id is not None else [])
        if not free:
            return self._offer_slots(session, conv, note="slot_taken")
        if len(free) == 1:
            return self._on_slot_chosen(session, conv, str(free[0][0]), start_iso)
        return self._ask_doctor(session, conv, start, free)

    def _ask_doctor(self, session: Session, conv: Conversation,
                    start: datetime, free, note: str | None = None) -> Reply:
        lang = self._lang(conv)
        minutes = int(start.timestamp()) // 60
        rows = [(Button(name or t("btn_any_doctor", lang),
                        f"d:{doctor_id}:{minutes}"),)
                for doctor_id, name in free]
        rows.append((Button(t("btn_any_doctor", lang), f"d:any:{minutes}"),))
        local = start.astimezone(self._clinic_tz(session))
        prefix = t(note, lang) + "\n" if note else ""
        return Reply(prefix + t("ask_doctor", lang, when=f"{local:%H:%M}"),
                     button_rows=tuple(rows))

    def _on_doctor_chosen(self, session: Session, conv: Conversation,
                          rest: str) -> Reply:
        """`d:<id врача|any>:<минуты эпохи>` — кнопка несёт и врача, и время."""
        lang = self._lang(conv)
        doctor_raw, _, minutes_raw = rest.partition(":")
        if not minutes_raw.isdigit():
            return self._with_reprompt(session, conv,
                                       Reply(t("stale_button", lang)))
        service_id = self._service_id(session, conv.context.service)
        try:
            start = datetime.fromtimestamp(int(minutes_raw) * 60,
                                           tz=self._clinic_tz(session))
        except (ValueError, OverflowError, OSError):
            # минуты приходят от клиента: переполнение роняло обработку, и
            # апдейт уходил в dead letter (та же защита, что у wl:take)
            return self._with_reprompt(session, conv,
                                       Reply(t("stale_button", lang)))
        free = (self._free_doctors_at(session, service_id, start,
                                      conv.context.doctor_id)
                if service_id is not None else [])
        if not free:
            return self._offer_slots(session, conv, note="slot_taken")
        chosen = free[0][0]
        if doctor_raw != "any":
            chosen = next((d for d, _ in free if str(d) == doctor_raw), None)
            if chosen is None:
                # выбранного врача заняли, пока пациент отвечал. Молча
                # посадить его к другому нельзя — он выбирал ЭТОГО: говорим
                # прямо и даём выбрать заново, даже если свободен один
                return self._ask_doctor(session, conv, start, free,
                                        note="doctor_taken")
        return self._on_slot_chosen(session, conv, str(chosen),
                                    start.isoformat())

    def _service_buttons(self, session: Session, lang: str) -> tuple[Button, ...]:
        names = services_repo.service_keys(session)
        return tuple(
            Button(f"{SERVICE_EMOJI.get(n, '🦷')} {service_label(n, lang)}",
                   f"service:{n}")
            for n in names)

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
        doctors = doctors_repo.doctor_list(session)
        if only_id:
            doctors = [d for d in doctors if str(d[0]) == only_id]
        return doctors

    # обрывок короче этого — не имя: «a» входит почти в любое имя и молча
    # привязал бы пациента к первому попавшемуся врачу
    _DOCTOR_NAME_MIN = 3

    def _resolve_doctor(self, session: Session, ctx: DialogContext,
                        name: str) -> None:
        target = name.casefold().strip()
        if len(target) >= self._DOCTOR_NAME_MIN:
            for doctor_id, doctor_name in self._doctors(session):
                if doctor_name and (target in doctor_name.casefold()
                                    or doctor_name.casefold() in target):
                    ctx.doctor_id = str(doctor_id)
                    return
        ctx.doctor_miss = True

    def _collect_slots(self, session: Session, doctors, service_id,
                       asked: date, time_ref: str | None):
        """Первый день (от asked, до 2 недель вперёд) с подходящими слотами."""
        tz = self._clinic_tz(session)
        start_day = max(asked, self._today(session))
        now_utc = self._clock()
        target = exact_time_ref(time_ref)  # точное HH:MM — мягкое предпочтение
        for offset in range(NEAREST_DAY_SCAN + 1):
            day = start_day + timedelta(days=offset)
            free = [
                (slot.start, doctor_id, doctor_name)
                for doctor_id, doctor_name in doctors
                for slot in self._sched.find_free_slots(doctor_id, service_id, day)
                if slot.start > now_utc  # сегодняшние прошедшие слоты не предлагаем
            ]
            strict = [s for s in free
                      if matches_time_ref(time_ref, s[0].astimezone(tz).time())]
            if strict:
                strict.sort(key=lambda s: (s[0], str(s[1])))
                return day, strict
            # некруглое время без совпадения с сеткой: не врать «нет слотов»,
            # показать слоты дня, ближайшие к запрошенному времени (M1)
            if target is not None and free:
                tmin = target.hour * 60 + target.minute
                free.sort(key=lambda s: (
                    abs(self._local_minutes(s[0], tz) - tmin), s[0], str(s[1])))
                return day, free
        return start_day, []

    @staticmethod
    def _local_minutes(start: datetime, tz) -> int:
        local = start.astimezone(tz).time()
        return local.hour * 60 + local.minute

    def _offer_body(self, session: Session, lang: str, asked: date, day: date,
                    time_ref: str | None = None) -> str:
        today = self._today(session)
        if max(asked, today) == today and self._closed_now(session):
            # пациент метит в «сегодня», а клиника вне рабочего окна: говорим
            # прямо «закрыто» — иначе ответ читается как «всё занято» (P0 BRIEF)
            return t("closed_now_slots", lang, date=f"{day:%d.%m}")
        prefix = self._outside_hours_line(session, lang, max(asked, today),
                                          time_ref)
        if day == asked:
            return prefix + t("offer_slots", lang, date=f"{day:%d.%m}")
        return prefix + t("offer_slots_other_day", lang, asked=f"{asked:%d.%m}",
                          date=f"{day:%d.%m}")

    def _outside_hours_line(self, session: Session, lang: str, day: date,
                            time_ref: str | None) -> str:
        """Точное запрошенное время вне рабочего окна дня → честная строка
        «Клиника работает с X до Y» перед слотами (П-3). Окна morning/evening
        не считаются; день целиком закрыт — существующий скан и так уводит
        на ближайший рабочий день."""
        target = exact_time_ref(time_ref)
        if target is None:
            return ""
        window = self._day_window(session, day)
        if window is None:
            return ""
        lo, hi = window
        if lo.time() <= target < hi.time():
            return ""
        return t("outside_hours", lang,
                 open=f"{lo:%H:%M}", close=f"{hi:%H:%M}") + "\n"

    def _day_window(self, session: Session, day: date):
        """Рабочее окно дня (union графиков врачей) в локальном времени
        клиники; None — день целиком закрыт."""
        tz = self._clinic_tz(session)
        schedules = doctors_repo.working_intervals(session)
        holidays = clinic_repo.holidays_on(session, day)
        bounds = open_bounds(schedules, day, tz, holidays)
        if bounds is None:
            return None
        lo, hi = bounds
        return lo.astimezone(tz), hi.astimezone(tz)

    def _closed_now(self, session: Session) -> bool:
        """Клиника вне рабочего окна прямо сейчас (выходной/праздник — тоже)."""
        today = self._today(session)
        schedules = doctors_repo.working_intervals(session)
        holidays = clinic_repo.holidays_on(session, today)
        bounds = open_bounds(schedules, today, self._clinic_tz(session), holidays)
        if bounds is None:
            return True
        lo, hi = bounds
        return not (lo <= self._clock() < hi)

    def _slot_label(self, start: datetime, doctor_name: str | None,
                    tz: ZoneInfo, multi_doctor: bool) -> str:
        local = start.astimezone(tz)
        label = f"{local:%d.%m %H:%M}"
        if multi_doctor and doctor_name:
            label += f" · {doctor_name}"
        return label
