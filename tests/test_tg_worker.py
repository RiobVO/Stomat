"""Воркер: апдейт из очереди → FSM → ответ с кнопками. Без реального API.

Здесь же — приёмочный сценарий BRIEF «3 сообщения за 2 сек → FSM не рвётся»
и greeting-дисклеймер первого контакта (P0).
"""
from __future__ import annotations

import json
import threading

from sqlalchemy import text

from conftest import next_monday
from navbat.db.base import tenant_transaction
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.replies import Reply, TEMPLATES
from navbat.nlu.extractor import FakeExtractor
from navbat.telegram.api import TelegramAPIError
from navbat.telegram.queue import enqueue
from navbat.telegram.worker import UpdateWorker
from test_dialog_booking import CHAT, RecordingNotifier, explicit, extr

UPDATE_SEQ = iter(range(1, 10_000))


class FakeTelegramAPI:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, tuple]] = []
        self.keyboards: list[tuple] = []  # (contact_request, remove_keyboard)
        self.answered: list[str] = []
        self.send_failures = 0  # сколько ближайших send уронить

    def send_message(self, chat_id, text, buttons=(),
                     contact_request=None, remove_keyboard=False):
        if self.send_failures > 0:
            self.send_failures -= 1
            raise TelegramAPIError("эмуляция падения сети")
        self.sent.append((chat_id, text, tuple(buttons)))
        self.keyboards.append((contact_request, remove_keyboard))
        return {"message_id": len(self.sent)}

    def answer_callback_query(self, callback_query_id):
        self.answered.append(callback_query_id)
        return True


def make_worker(app_session_factory, clinic_id, script,
                api: FakeTelegramAPI | None = None,
                notifier: RecordingNotifier | None = None,
                admin_chat_id: int | None = None):
    api = api or FakeTelegramAPI()
    notifier = notifier or RecordingNotifier()
    dialog = DialogEngine(app_session_factory, clinic_id,
                          extractor=FakeExtractor(script=script), notifier=notifier)
    worker = UpdateWorker(app_session_factory, clinic_id, dialog=dialog,
                          api=api, notifier=notifier, admin_chat_id=admin_chat_id)
    return worker, api, notifier


def put_message(app_session_factory, clinic_id, text_in, chat_id=CHAT):
    update_id = next(UPDATE_SEQ)
    payload = {"update_id": update_id,
               "message": {"chat": {"id": chat_id}, "text": text_in}}
    if text_in is None:
        del payload["message"]["text"]
        payload["message"]["sticker"] = {"emoji": "🦷"}
    with tenant_transaction(app_session_factory, clinic_id) as session:
        enqueue(session, update_id, chat_id, payload)


def put_callback(app_session_factory, clinic_id, data, chat_id=CHAT):
    update_id = next(UPDATE_SEQ)
    payload = {"update_id": update_id,
               "callback_query": {"id": f"cq{update_id}", "data": data,
                                  "message": {"chat": {"id": chat_id}}}}
    with tenant_transaction(app_session_factory, clinic_id) as session:
        enqueue(session, update_id, chat_id, payload)


def context_of(admin_engine, chat_id=CHAT) -> dict:
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT context FROM conversation WHERE tg_chat_id = :c"),
            {"c": chat_id},
        ).scalar_one()


def queue_statuses(admin_engine) -> list[str]:
    with admin_engine.begin() as conn:
        return conn.execute(
            text("SELECT status FROM message_queue ORDER BY update_id")
        ).scalars().all()


# ── Кнопки: короткий callback_data + map в контексте ─────────────────────────

def test_message_reply_uses_short_callback_data(app_session_factory, admin_engine,
                                                clinic_a, doctor_a, service_cleaning):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [
        extr(service="cleaning", date_ref=explicit(next_monday())),
    ])
    put_message(app_session_factory, clinic_a, "чистку в понедельник")
    assert worker.process_one() is True

    chat_id, _, buttons = api.sent[0]
    assert chat_id == CHAT
    assert buttons, "ожидались кнопки слотов"
    for button in buttons:
        assert len(button.action.encode()) <= 64, "лимит callback_data Telegram"
        assert button.action.startswith("a:")
    actions_map = context_of(admin_engine)["tg_actions"]
    assert actions_map["1"].startswith("slot:")
    assert queue_statuses(admin_engine) == ["done"]


def test_callback_executes_mapped_action(app_session_factory, admin_engine,
                                         clinic_a, doctor_a, service_cleaning):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [
        extr(service="cleaning", date_ref=explicit(next_monday())),
    ])
    put_message(app_session_factory, clinic_a, "чистку в понедельник")
    worker.process_one()

    put_callback(app_session_factory, clinic_a, "a:1")  # первый слот
    assert worker.process_one() is True

    assert api.answered, "callback_query должен быть подтверждён (часики)"
    # новый пациент → после выбора слота спрашиваем имя
    assert api.sent[-1][1] == TEMPLATES["ask_name"]["ru"]
    with admin_engine.begin() as conn:
        status = conn.execute(text("SELECT status FROM appointment")).scalar_one()
    assert status == "hold"


def test_stale_button_reprompts_current_step(app_session_factory, admin_engine,
                                             clinic_a, doctor_a, service_cleaning):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [
        extr(service="cleaning", date_ref=explicit(next_monday())),
    ])
    put_message(app_session_factory, clinic_a, "чистку в понедельник")
    worker.process_one()

    put_callback(app_session_factory, clinic_a, "a:99")  # такой кнопки нет в map
    worker.process_one()

    _, reply_text, buttons = api.sent[-1]
    assert TEMPLATES["stale_button"]["ru"] in reply_text
    assert buttons, "повтор шага: слоты предложены заново"


# ── Greeting-дисклеймер ──────────────────────────────────────────────────────

def test_greeting_disclaimer_on_first_contact_only(app_session_factory, clinic_a,
                                                   doctor_a, service_cleaning):
    day = explicit(next_monday())
    worker, api, _ = make_worker(app_session_factory, clinic_a, [
        extr(service="cleaning", date_ref=day),
        extr(service="cleaning", date_ref=day, time_ref="morning"),
    ])
    put_message(app_session_factory, clinic_a, "чистку в понедельник")
    worker.process_one()
    put_message(app_session_factory, clinic_a, "лучше утром")
    worker.process_one()

    greeting_marker = "виртуальный администратор"
    assert greeting_marker in api.sent[0][1].lower()
    assert greeting_marker not in api.sent[1][1].lower()


# ── Контакт: телефон кнопкой request_contact ─────────────────────────────────

class RecordingDialog:
    """Стаб FSM: фиксирует вызовы handle_contact, отвечает заданным Reply."""

    def __init__(self, reply: Reply) -> None:
        self.reply = reply
        self.contacts: list[tuple[int, str, bool]] = []

    def handle_contact(self, chat_id, phone, own):
        self.contacts.append((chat_id, phone, own))
        return self.reply


def put_contact(app_session_factory, clinic_id, phone, contact_user_id,
                from_id, chat_id=CHAT):
    update_id = next(UPDATE_SEQ)
    contact = {"phone_number": phone}
    if contact_user_id is not None:
        contact["user_id"] = contact_user_id
    payload = {"update_id": update_id,
               "message": {"chat": {"id": chat_id}, "from": {"id": from_id},
                           "contact": contact}}
    with tenant_transaction(app_session_factory, clinic_id) as session:
        enqueue(session, update_id, chat_id, payload)


def contact_worker(app_session_factory, clinic_id, reply: Reply):
    api = FakeTelegramAPI()
    dialog = RecordingDialog(reply)
    worker = UpdateWorker(app_session_factory, clinic_id, dialog=dialog, api=api)
    return worker, api, dialog


def test_contact_update_routed_with_own_flag(app_session_factory, admin_engine,
                                             clinic_a, doctor_a, service_cleaning):
    worker, api, dialog = contact_worker(
        app_session_factory, clinic_a,
        Reply("Записал!", remove_keyboard=True))
    put_contact(app_session_factory, clinic_a, "998901234567",
                contact_user_id=CHAT, from_id=CHAT)
    worker.process_one()

    assert dialog.contacts == [(CHAT, "998901234567", True)]
    assert api.keyboards[-1] == (None, True), "remove_keyboard дошёл до API"
    assert queue_statuses(admin_engine) == ["done"]


def test_foreign_or_missing_user_id_not_own(app_session_factory, admin_engine,
                                            clinic_a, doctor_a, service_cleaning):
    reply = Reply("Нажмите кнопку:", contact_request="📱")
    worker, api, dialog = contact_worker(app_session_factory, clinic_a, reply)

    put_contact(app_session_factory, clinic_a, "998905555555",
                contact_user_id=999, from_id=CHAT)   # чужой контакт
    put_contact(app_session_factory, clinic_a, "998905555555",
                contact_user_id=None, from_id=CHAT)  # контакт не из Telegram
    worker.process_one()
    worker.process_one()

    assert [own for _, _, own in dialog.contacts] == [False, False]
    assert api.keyboards[-1] == ("📱", False), "кнопка предложена снова"


# ── Не-текст и служебные апдейты ─────────────────────────────────────────────

def test_non_text_message_gets_stub_reply(app_session_factory, admin_engine,
                                          clinic_a, doctor_a, service_cleaning):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [])
    put_message(app_session_factory, clinic_a, None)  # стикер
    worker.process_one()
    assert len(api.sent) == 1
    assert queue_statuses(admin_engine) == ["done"]


def test_service_update_completed_silently(app_session_factory, admin_engine,
                                           clinic_a, doctor_a, service_cleaning):
    update_id = next(UPDATE_SEQ)
    with tenant_transaction(app_session_factory, clinic_a) as session:
        enqueue(session, update_id, CHAT,
                {"update_id": update_id, "my_chat_member": {"chat": {"id": CHAT}}})
    worker, api, _ = make_worker(app_session_factory, clinic_a, [])
    worker.process_one()
    assert api.sent == []
    assert queue_statuses(admin_engine) == ["done"]


# ── Надёжность: ack после обработки, dead letter ─────────────────────────────

def test_send_failure_returns_update_to_pending(app_session_factory, admin_engine,
                                                clinic_a, doctor_a, service_cleaning):
    day = explicit(next_monday())
    api = FakeTelegramAPI()
    api.send_failures = 1
    worker, api, _ = make_worker(app_session_factory, clinic_a, [
        extr(service="cleaning", date_ref=day),
        extr(service="cleaning", date_ref=day),  # повтор после retry
    ], api=api)
    put_message(app_session_factory, clinic_a, "чистку в понедельник")

    worker.process_one()  # send упал
    assert queue_statuses(admin_engine) == ["pending"], "ack не выдан — апдейт жив"

    worker.process_one()  # повтор успешен
    assert queue_statuses(admin_engine) == ["done"]
    assert len(api.sent) == 1


def test_dead_letter_notifies_admin(app_session_factory, admin_engine, clinic_a,
                                    doctor_a, service_cleaning):
    day = explicit(next_monday())
    api = FakeTelegramAPI()
    api.send_failures = 3
    notifier = RecordingNotifier()
    worker, api, notifier = make_worker(
        app_session_factory, clinic_a,
        [extr(service="cleaning", date_ref=day)] * 3, api=api, notifier=notifier)
    put_message(app_session_factory, clinic_a, "чистку в понедельник")

    for _ in range(3):
        worker.process_one()
    assert queue_statuses(admin_engine) == ["failed"]
    assert any("dead letter" in reason for _, reason in notifier.calls)


# ── Приёмочный BRIEF: 3 сообщения подряд, FSM не рвётся ──────────────────────

def test_three_rapid_messages_keep_fsm_consistent(app_session_factory, admin_engine,
                                                  clinic_a, doctor_a, service_cleaning):
    day = explicit(next_monday())
    worker, api, _ = make_worker(app_session_factory, clinic_a, [
        extr(service="cleaning"),                      # 1: без даты → вопрос дня
        extr(date_ref=day),                            # 2: дата → слоты
        extr(date_ref=day, time_ref="morning"),        # 3: уточнение → слоты утра
    ])
    put_message(app_session_factory, clinic_a, "хочу чистку")
    put_message(app_session_factory, clinic_a, "в понедельник")
    put_message(app_session_factory, clinic_a, "лучше утром")

    stop = threading.Event()

    def crank():
        while not stop.is_set():
            if not worker.process_one():
                stop.wait(0.01)

    threads = [threading.Thread(target=crank) for _ in range(4)]
    for thread in threads:
        thread.start()
    for _ in range(500):
        if queue_statuses(admin_engine) == ["done"] * 3:
            break
        threading.Event().wait(0.02)
    stop.set()
    for thread in threads:
        thread.join(timeout=5)

    assert queue_statuses(admin_engine) == ["done"] * 3
    assert len(api.sent) == 3
    assert api.sent[0][1].endswith(TEMPLATES["ask_date"]["ru"]), "шаг 1: вопрос дня"
    assert any(b.action == "a:1" for b in api.sent[1][2]), "шаг 2: слоты"
    # шаг 3: state не разорван — это снова предложение слотов, не сброс
    with admin_engine.begin() as conn:
        state = conn.execute(text("SELECT fsm_state FROM conversation")).scalar_one()
    assert state == "booking_offer_slots"
