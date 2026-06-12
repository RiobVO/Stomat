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
- эскалация (стоп-состояние escalated) — только по прямой просьбе человека
  или при двойном сбое подтверждения записи (П-2а); кривые ответы NLU и
  вопросы вне компетенции дают «не понял» + кнопки, админа не дёргают.
"""
from __future__ import annotations

import uuid
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, sessionmaker

from navbat.crypto import encrypt_text
from navbat.db.base import tenant_transaction
from navbat.dialog import clinic_repo, doctors_repo, questions_repo, services_repo
from navbat.dialog.booking_flow import _BookingFlowMixin
from navbat.dialog.calendar_flow import _CalendarFlowMixin
from navbat.dialog.cancel_flow import _CancelFlowMixin
from navbat.dialog.conversation import (
    Conversation, load_conversation, save_conversation)
from navbat.dialog.dialog_common import (
    MAX_NLU_FAILURES,
    NEAREST_DAY_SCAN,
    SlotGuard,
    looks_uzbek_cyrillic,
    mentions_address_question,
    mentions_availability,
    mentions_hours_question,
    mentions_human_request,
    mentions_payment_question,
    mentions_phone_question,
    mentions_price_question,
    mentions_symptom,
)
from navbat.dialog.escalation import EscalationNotifier, LoggingEscalation
from navbat.dialog.patients import normalize_phone, phone_to_hash
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
from navbat.nlu.extractor import ExtractionError, Extractor, LLMDisabledError
from navbat.nlu.schema import Extraction
from navbat.nlu.wrappers import redact_phones
from navbat.scheduling.calendar_rules import open_bounds
from navbat.scheduling.engine import SchedulingEngine
from navbat.scheduling.errors import AppointmentNotFoundError

_MENU_KEYS = ("btn_menu_book", "btn_menu_resched", "btn_menu_cancel",
              "btn_menu_prices", "btn_menu_about", "btn_menu_lang")
# нажатие reply-кнопки приходит ТЕКСТОМ — матчим label'ы обоих языков
_MENU_ACTIONS = {
    TEMPLATES[key][lang]: key for key in _MENU_KEYS for lang in ("ru", "uz")
}
# шаги сценария, где у бота «на руках» конкретный вопрос (услуга/день/слот):
# непонятый текст здесь повторяет текущий шаг, а не прыгает по сценарию
_SCENARIO_STATES = ("booking_collect", "booking_offer_slots",
                    "resched_offer_slots")


class DialogEngine(_SharedHelpersMixin, _BookingFlowMixin,
                   _RescheduleFlowMixin, _CancelFlowMixin, _CalendarFlowMixin):
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
        # дедуп FYI «нет слотов на 2 недели» (П-5): раз в день, в памяти
        self._no_slots_fyi_date: object | None = None

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
        """Сырой номер (демо / прямой вызов): хэшируем, шифруем и делегируем
        хэш-пути. В боевом потоке телефон хэшируется и шифруется на границе
        enqueue и открытым сюда не попадает; этот вход остаётся для каналов
        без durable-очереди."""
        try:
            normalized: str | None = normalize_phone(phone)
        except ValueError:
            normalized = None  # номер не распознан → повтор кнопки
        if normalized is None:
            return self.handle_contact_hashed(chat_id, None, None, own)
        # hash/encrypt вне except-зоны: binascii.Error от кривого NAVBAT_ENC_KEY
        # (подкласс ValueError) не должен маскироваться под «номер не распознан»
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            phone_hash = phone_to_hash(session, normalized)
        return self.handle_contact_hashed(chat_id, phone_hash,
                                          encrypt_text(normalized), own)

    def handle_contact_hashed(self, chat_id: int, phone_hash: str | None,
                              phone_encrypted: str | None, own: bool) -> Reply:
        """Контакт из кнопки «Поделиться»: телефон уже хэширован и зашифрован
        (открытый номер в очередь не попал; шифртекст — пересмотр 11.06,
        номер нужен владельцу в календаре). Принимается номер любой страны
        (П-2в); phone_hash=None — номер не распознан; own — собственный
        контакт."""
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            conv = load_conversation(session, chat_id)
            reply = self._process_contact(session, conv, phone_hash,
                                          phone_encrypted, own)
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
        # меню = пациент сориентировался: серия «не понял» прервана (П-2а)
        conv.context.nlu_failures = 0
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
        if key == "btn_menu_about":
            return self._with_reprompt(session, conv,
                                       self._about_clinic(session, conv))
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
            # детектор просьбы человека здесь выключен: любой текст может
            # быть именем («Оператор Умаров» — редкое, но имя)
            return self._on_name(session, conv, message)
        if mentions_human_request(message):
            # прямая просьба позвать человека — единственный текстовый путь
            # к эскалации (П-2а); ловится ДО NLU: ноль токенов и работает
            # даже при лежащем LLM
            return self._escalate_on_request(session, conv)
        if conv.state == "awaiting_phone":
            return self._on_phone(session, conv, message)
        if conv.context.lang is not None:
            # FAQ до LLM (живая находка 12.06): часы/адрес/оплата/телефон —
            # регэкспы, ноль токенов (обещание DEMO.md); заодно «до скольки
            # работаете сегодня?» не угоняется booking_like-бэкстопом в слоты.
            # Первый контакт (lang ещё не выбран) идёт через NLU — его детект
            # языка нужен для ответа.
            faq = self._faq_answer(session, conv, message)
            if faq is not None:
                return self._with_reprompt(session, conv, faq)

        try:
            extraction = self._extractor.extract(message)
        except LLMDisabledError:
            # рубильник /llm off: свободный текст без NLU — мягко в кнопки,
            # счётчик сбоев не трогаем (это режим, не сбой)
            return Reply(t("llm_off_menu", lang), menu=menu_rows(lang))
        except ExtractionError:
            return self._on_nlu_failure(session, conv)
        if conv.context.lang is None:
            # язык ещё не выбран (первый контакт свободным текстом) — берём
            # детект NLU; дальше язык меняет ТОЛЬКО кнопка «Til / Язык»:
            # явный выбор пациента главнее самого слабого поля модели
            # (узбекскую кириллицу NLU массово зовёт ru — eval 12.06.2026).
            # Буквы ўқғҳ — детерминированный признак узбекского, перебивают
            # детект (живой тест 12.06: «Тишим оғрияпти» получал ru-интерфейс)
            if looks_uzbek_cyrillic(message):
                conv.context.lang = "uz"
            else:
                conv.context.lang = ("ru" if extraction.language == "mixed"
                                     else extraction.language)
        lang = self._lang(conv)

        failures_before = conv.context.nlu_failures
        reply = self._route_intent(session, conv, extraction, message)
        if conv.context.nlu_failures == failures_before:
            # сброс ПОСЛЕ маршрутизации: валидный intent у мусора («уыкп» →
            # other) — ещё не понимание; непонятое посреди сценария копит
            # счётчик той же машинерией, что ExtractionError (пересмотр 11.06)
            conv.context.nlu_failures = 0
        return self._with_medical_disclaimer(conv, extraction, reply, message)

    def _route_intent(self, session: Session, conv: Conversation,
                      extraction: Extraction, message: str) -> Reply:
        # бэкстоп: дыра NLU book↔{question,other} — «есть время сегодня?»
        # уходит в question, голое «на завтра» в other (живой пример);
        # реплика с привязкой ко времени == про запись, ведём сценарий
        booking_like = extraction.intent == "book" or (
            extraction.intent in ("question", "other")
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
            price_note = self._combined_price_note(session, conv,
                                                   extraction, message)
            reply = self._continue_booking(session, conv, extraction)
            if price_note is not None:
                reply = replace(reply, text=f"{price_note}\n\n{reply.text}")
            return reply
        if extraction.intent in ("question", "other"):
            # FAQ-слой (П-2б) ДО детектора наличия: «ish vaqti?» содержит
            # маркер «vaqt», но это вопрос о часах, не о слотах
            faq = self._faq_answer(session, conv, message)
            if faq is None and not extraction.service \
                    and mentions_price_question(message):
                # «narxlari qancha» без услуги — прайс целиком, не «не понял»
                # (живая батарея 12.06); вопрос с услугой дойдёт до точечного
                # ответа в _answer_question
                faq = self._price_list(session, conv)
            if faq is not None:
                return self._with_reprompt(session, conv, faq)
        if extraction.intent in ("question", "other") and not extraction.service \
                and self._asks_availability(conv, message):
            # вопрос о наличии без даты («а ещё?», «другой день?») — выбор
            # дня, не эскалация: на вопрос о наличии бот ВСЕГДА отвечает
            # слотами (П-1, та же философия, что и book↔question бэкстоп)
            return self._availability_reply(session, conv)
        if extraction.intent in ("question", "other") and not extraction.service \
                and conv.state in _SCENARIO_STATES:
            # пересмотр 11.06 (живой тест): непонятое посреди сценария
            # («привет», мусор) — не вопрос о наличии и не повод терять шаг:
            # повтор текущего шага машинерией сбоев (2-й раз — кнопка к
            # человеку); в копилку вопросов не пишем — это не вопрос владельцу
            return self._on_nlu_failure(session, conv)
        if extraction.intent == "question":
            answer = self._answer_question(session, conv, extraction, message)
            return self._with_reprompt(session, conv, answer)
        if extraction.intent == "reschedule":
            return self._start_reschedule(session, conv, extraction)
        if extraction.intent == "cancel":
            return self._start_cancel(session, conv)
        # бессодержательный other («ыыыыы» живой LLM парсит как валидный
        # other — живой тык 12.06) = тот же «не понял», что ExtractionError:
        # 1-й раз переспрос с меню (M7), 2-й — кнопка к человеку (DEMO.md
        # шаг 10 теперь честен и в пустом чате)
        return self._on_nlu_failure(session, conv)

    def _asks_availability(self, conv: Conversation, message: str) -> bool:
        """Это вопрос о наличии? Только явные сигналы (П-1, пересмотр 11.06).

        Посреди сценария — ТОЛЬКО словарный маркер: старое правило «любой
        текст после показа слотов = про наличие» живой тест опроверг (мусор
        и «привет» прыгали на выбор дня с дефолтным осмотром); дату/время
        прикрывает бэкстоп booking_like. Вне сценария: услуга/дата ещё
        в контексте (доуточнение после показа слотов) ИЛИ словарь."""
        if conv.state in _SCENARIO_STATES:
            return mentions_availability(message)
        if conv.context.service or conv.context.date:
            return True
        return mentions_availability(message)

    def _availability_reply(self, session: Session, conv: Conversation) -> Reply:
        self._ensure_service(session, conv)  # дефолт checkup, как в бэкстопе
        return self._ask_date(session, conv)

    def _ask_date(self, session: Session, conv: Conversation) -> Reply:
        """Выбор дня (кнопка «Другое время» и вопросы о наличии):
        ближайшие дни кнопками + вход в инлайн-календарь (П-5)."""
        lang = self._lang(conv)
        if not conv.context.resched_id:
            conv.state = "booking_collect"
        today = self._today(session)
        buttons = self._date_buttons(session, lang) + (
            Button(t("btn_pick_date", lang), f"cal:nav:{today.isoformat()}"),)
        return Reply(t("ask_date", lang), buttons)

    def _escalate_on_request(self, session: Session, conv: Conversation) -> Reply:
        """Эскалация по прямой просьбе пациента: заморозка + алерт.

        notify ДО _abort_pending — контекст сценария должен доехать до
        админа целым; висящий hold отпускаем, бронь просьбу не переживает."""
        self._notifier.notify(conv.chat_id, "пациент просит администратора",
                              self._escalation_context(conv))
        self._abort_pending(conv)
        conv.state = "escalated"
        # вне рабочего окна честно говорим «утром» — иначе пациент ждёт
        # ответа ночью
        key = "escalated_closed" if self._closed_now(session) else "escalated"
        return Reply(t(key, self._lang(conv)))

    def _on_nlu_failure(self, session: Session, conv: Conversation) -> Reply:
        lang = self._lang(conv)
        failures = conv.context.nlu_failures + 1
        conv.context.nlu_failures = failures
        if failures >= MAX_NLU_FAILURES:
            # NLU лежит или текст нечитаем — НЕ эскалируем (П-2а): повторяем
            # текущий шаг кнопками, кнопочный путь работает без LLM (C-4);
            # кнопка «позвать администратора» вместо menu (reply_markup один):
            # reply-меню у пациента и так на экране (is_persistent)
            note = Reply(t("not_understood", lang),
                         (Button(t("btn_call_admin", lang), "call_admin"),))
            reply = self._with_reprompt(session, conv, note)
            if not any(b.action == "call_admin" for b in reply.buttons):
                # посреди сценария _with_reprompt отдал кнопки шага — выход
                # к человеку дополняет их, а не теряется (пересмотр 11.06)
                reply = replace(reply, buttons=reply.buttons + note.buttons)
            return reply
        # 1-й сбой посреди сценария — повтор текущего шага (пересмотр 11.06);
        # вне сценария не понятому пациенту — кнопки самообслуживания (M7)
        return self._with_reprompt(session, conv,
                                   Reply(t("reask", lang), menu=menu_rows(lang)))

    def _with_medical_disclaimer(self, conv: Conversation, extraction: Extraction,
                                 reply: Reply, message: str) -> Reply:
        # дисклеймер уместен при жалобе или мед-вопросе; просьба об услуге
        # без симптома («rentgen kerak» — живая батарея 12.06) обходится:
        # is_medical модели здесь шире, чем нужно, фильтр — код-слой
        applicable = extraction.intent == "question" or mentions_symptom(message)
        if extraction.is_medical and applicable and not conv.context.medical_shown:
            conv.context.medical_shown = True
            disclaimer = MEDICAL_DISCLAIMER[self._lang(conv)]
            return Reply(f"{disclaimer}\n\n{reply.text}", reply.buttons)
        return reply

    # ── Кнопки (callback-actions) ────────────────────────────────────────

    def _process_action(self, session: Session, conv: Conversation, action: str) -> Reply:
        lang = self._lang(conv)
        kind, _, rest = action.partition(":")
        if kind == "attend":
            # «Приду» из напоминания — чистое подтверждение, состояние не
            # меняет; работает и в escalated (живой тест 12.06: пациент позвал
            # человека, потом пришло напоминание — тап не должен отвечать
            # «передаю администратору»)
            return Reply(t("attend_ok", lang))
        if conv.state == "escalated":
            return Reply(t("escalated", lang))
        if kind == "call_admin":
            # кнопка из фоллбэка = ровно путь «позовите администратора»;
            # повторный клик не дублирует алерт: escalated перехвачен выше
            return self._escalate_on_request(session, conv)
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
            return self._ask_date(session, conv)
        if kind == "slot":
            doctor_id, _, start_iso = rest.partition(":")
            return self._on_slot_chosen(session, conv, doctor_id, start_iso)
        if kind == "reslot":
            return self._on_reslot(session, conv, rest)
        if kind == "wl":
            return self._on_waitlist(session, conv, rest)
        if kind == "cancel_yes":
            return self._on_cancel_confirmed(session, conv)
        if kind == "cancel_no":
            self._clear_booking(conv)
            conv.state = "idle"
            return Reply(t("cancel_kept", lang))
        if kind == "stale":
            # нажата кнопка из устаревшего сообщения — повторяем текущий шаг
            return self._with_reprompt(session, conv, Reply(t("stale_button", lang)))
        if kind == "remind_cancel":
            return self._start_cancel_by_id(session, conv, rest)
        if kind == "cal":
            # инлайн-календарь (П-5): сырые короткие callback'и мимо map'а
            return self._on_calendar(session, conv, rest)
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

    def _combined_price_note(self, session: Session, conv: Conversation,
                             extraction: Extraction, message: str) -> str | None:
        """«Запись + вопрос цены» одним сообщением («завтра пломба, qancha
        turadi?» — живой транскрипт S04 12.06): ценовая половина не теряется,
        строка цены встаёт перед слотами."""
        service = extraction.service or conv.context.service
        if service is None or not mentions_price_question(message):
            return None
        lang = self._lang(conv)
        label = service_label(service, lang)
        price = services_repo.service_price(session, service)
        if price is None:
            return t("price_unknown", lang, service=label)
        formatted = f"{int(price):,}".replace(",", " ")
        return t("price_answer", lang, service=label, price=formatted)

    def _answer_question(self, session: Session, conv: Conversation,
                         extraction: Extraction, message: str) -> Reply:
        """Цена — из каталога; на прочее бот честно отвечает «не понял».
        Состояние диалога вопрос не меняет."""
        lang = self._lang(conv)
        if extraction.service:
            label = service_label(extraction.service, lang)
            price = services_repo.service_price(session, extraction.service)
            if price is None:
                return Reply(t("price_unknown", lang, service=label))
            formatted = f"{int(price):,}".replace(",", " ")
            return Reply(t("price_answer", lang, service=label, price=formatted))
        # вопрос вне компетенции: «не понял» + кнопка к человеку, БЕЗ алерта
        # (П-2а) — админа зовёт только сам пациент; текст вопроса копится
        # анонимно (телефоны замаскированы) и придёт в дайджесте (П-2б)
        questions_repo.add(session, redact_phones(message))
        return Reply(t("not_understood", lang),
                     (Button(t("btn_call_admin", lang), "call_admin"),))

    def _faq_answer(self, session: Session, conv: Conversation,
                    message: str) -> Reply | None:
        """Бытовые вопросы (часы/адрес/оплата/телефон) — ответ без LLM
        и без админа (П-2б, полировка-2).

        None — не FAQ (или поле не задано): путь идёт дальше штатно."""
        if mentions_hours_question(message):
            return self._hours_reply(session, conv)
        if mentions_address_question(message):
            address = clinic_repo.clinic_address(session)
            if address:
                return Reply(t("clinic_address", self._lang(conv),
                               address=address))
        if mentions_payment_question(message):
            info = clinic_repo.clinic_payment_info(session)
            if info:
                return Reply(t("clinic_payment", self._lang(conv), info=info))
        if mentions_phone_question(message):
            phone = clinic_repo.clinic_phone(session)
            if phone:
                return Reply(t("clinic_phone", self._lang(conv), phone=phone))
        return None

    def _hours_reply(self, session: Session, conv: Conversation) -> Reply | None:
        """Рабочее окно сегодня (union графиков врачей); сегодня закрыто —
        ближайший рабочий день в горизонте двух недель."""
        lang = self._lang(conv)
        tz = self._clinic_tz(session)
        schedules = doctors_repo.working_intervals(session)
        today = self._today(session)
        for offset in range(NEAREST_DAY_SCAN + 1):
            day = today + timedelta(days=offset)
            bounds = open_bounds(schedules, day, tz,
                                 clinic_repo.holidays_on(session, day))
            if bounds is None:
                continue
            lo, hi = (b.astimezone(tz) for b in bounds)
            if offset == 0:
                return Reply(t("hours_today", lang,
                               open=f"{lo:%H:%M}", close=f"{hi:%H:%M}"))
            return Reply(t("hours_next", lang, date=f"{day:%d.%m}",
                           open=f"{lo:%H:%M}", close=f"{hi:%H:%M}"))
        return None  # графиков нет вообще — честное «не понял» дальше

    def _about_clinic(self, session: Session, conv: Conversation) -> Reply:
        """Карточка «ℹ️ О клинике» (полировка-2): часы + только заполненные
        поля; пустые строки не рендерятся — карточка не выглядит дырявой."""
        lang = self._lang(conv)
        lines = []
        hours = self._hours_reply(session, conv)
        if hours is not None:
            lines.append(hours.text)
        address = clinic_repo.clinic_address(session)
        if address:
            lines.append(t("clinic_address", lang, address=address))
        payment = clinic_repo.clinic_payment_info(session)
        if payment:
            lines.append(t("clinic_payment", lang, info=payment))
        phone = clinic_repo.clinic_phone(session)
        if phone:
            lines.append(t("clinic_phone", lang, phone=phone))
        header = t("about_header", lang, clinic=self._clinic_name(session))
        if not lines:
            return Reply(header)
        return Reply(header + "\n\n" + "\n".join(lines))

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
