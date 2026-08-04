"""Кнопочная инфраструктура (П-4): многорядная inline-клавиатура,
редактирование сообщения на месте, toast, сырые cal:-callback'и.

Фундамент инлайн-календаря (П-5); поведение диалога не меняется.
"""
from __future__ import annotations

import json

import httpx
from sqlalchemy import text

from navbat.db.base import tenant_transaction
from navbat.dialog.replies import Button, Reply
from navbat.telegram.api import TelegramAPI
from navbat.telegram.queue import enqueue
from navbat.telegram.worker import UpdateWorker, edit_reply, send_reply
from test_tg_api import make_api, ok_response
from test_tg_worker import CHAT, FakeTelegramAPI, RecordingNotifier, put_callback


# ── api-слой ─────────────────────────────────────────────────────────────────

def test_send_message_with_button_rows():
    api, requests = make_api(lambda req, n: ok_response({"message_id": 1}))
    api.send_message(100, "Календарь:",
                     button_rows=((Button("Пн", "cal:noop"), Button("Вт", "cal:noop")),
                                  (Button("1", "cal:day:2026-06-01"),)))

    keyboard = json.loads(requests[0].content)["reply_markup"]["inline_keyboard"]
    assert keyboard == [
        [{"text": "Пн", "callback_data": "cal:noop"},
         {"text": "Вт", "callback_data": "cal:noop"}],
        [{"text": "1", "callback_data": "cal:day:2026-06-01"}],
    ]


def test_edit_message_text_builds_request():
    api, requests = make_api(lambda req, n: ok_response({"message_id": 7}))
    api.edit_message_text(100, 7, "Июль 2026",
                          button_rows=((Button("◀", "cal:nav:2026-06"),),))

    request = requests[0]
    assert request.url.path.endswith("/editMessageText")
    body = json.loads(request.content)
    assert body["chat_id"] == 100 and body["message_id"] == 7
    assert body["reply_markup"]["inline_keyboard"][0][0]["callback_data"] \
        == "cal:nav:2026-06"


def test_edit_swallows_not_modified_error():
    def handler(req, n):
        return httpx.Response(400, json={
            "ok": False, "description":
            "Bad Request: message is not modified"})
    api, _ = make_api(handler)
    assert api.edit_message_text(100, 7, "тот же текст") is None  # не упало


def test_edit_raises_other_errors():
    import pytest
    from navbat.telegram.api import TelegramAPIError

    def handler(req, n):
        return httpx.Response(400, json={
            "ok": False, "description": "Bad Request: message to edit not found"})
    api, _ = make_api(handler)
    with pytest.raises(TelegramAPIError):
        api.edit_message_text(100, 7, "текст")


def test_answer_callback_query_with_toast():
    api, requests = make_api(lambda req, n: ok_response(True))
    api.answer_callback_query("cb1", text="Свободного времени нет")
    body = json.loads(requests[0].content)
    assert body["callback_query_id"] == "cb1"
    assert body["text"] == "Свободного времени нет"


def test_answer_callback_query_without_toast_omits_text():
    api, requests = make_api(lambda req, n: ok_response(True))
    api.answer_callback_query("cb1")
    assert "text" not in json.loads(requests[0].content)


# ── send_reply: нумерация по рядам + cal:-passthrough ────────────────────────

def tg_actions(admin_engine, chat_id=CHAT) -> dict:
    with admin_engine.begin() as conn:
        ctx = conn.execute(
            text("SELECT context FROM conversation WHERE tg_chat_id = :c"),
            {"c": chat_id}).scalar_one_or_none()
    return (ctx or {}).get("tg_actions", {})


def seed_conversation(app_session_factory, clinic_id, chat_id=CHAT):
    from navbat.dialog.conversation import Conversation, save_conversation
    with tenant_transaction(app_session_factory, clinic_id) as session:
        save_conversation(session, Conversation(chat_id=chat_id))


def test_send_reply_rows_number_long_actions_and_keep_cal_raw(
        app_session_factory, admin_engine, clinic_a):
    seed_conversation(app_session_factory, clinic_a)
    api = FakeTelegramAPI()
    reply = Reply("Слоты дня:", button_rows=(
        (Button("10:00", "slot:doctor-uuid:2026-06-15T10:00:00+05:00"),
         Button("10:30", "slot:doctor-uuid:2026-06-15T10:30:00+05:00")),
        (Button("◀ Календарь", "cal:nav:2026-06"),),
    ))
    send_reply(api, app_session_factory, clinic_a, CHAT, reply)

    rows = api.row_keyboards[-1]
    assert [b.action for b in rows[0]] == ["a:1", "a:2"], "длинные нумеруются"
    assert rows[1][0].action == "cal:nav:2026-06", "cal: уходит сырым"
    mapping = tg_actions(admin_engine)
    assert mapping == {
        "1": "slot:doctor-uuid:2026-06-15T10:00:00+05:00",
        "2": "slot:doctor-uuid:2026-06-15T10:30:00+05:00",
    }, "map только для нумерованных"


def test_send_reply_flat_buttons_unchanged(app_session_factory, admin_engine,
                                           clinic_a):
    seed_conversation(app_session_factory, clinic_a)
    api = FakeTelegramAPI()
    send_reply(api, app_session_factory, clinic_a, CHAT,
               Reply("Слоты:", buttons=(Button("10:00", "slot:d:t"),)))
    assert api.sent[-1][2][0].action == "a:1"
    assert tg_actions(admin_engine) == {"1": "slot:d:t"}


# ── Воркер: raw cal:, edit, toast ────────────────────────────────────────────

class SpyDialog:
    """Записывает actions; отвечает заданным Reply."""

    def __init__(self, reply: Reply) -> None:
        self.actions: list[str] = []
        self.reply = reply

    def handle_action(self, chat_id: int, action: str) -> Reply:
        self.actions.append(action)
        return self.reply


def run_callback(app_session_factory, clinic_a, dialog, data):
    api = FakeTelegramAPI()
    worker = UpdateWorker(app_session_factory, clinic_a, dialog=dialog,
                          api=api, notifier=RecordingNotifier())
    put_callback(app_session_factory, clinic_a, data)
    worker.process_one()
    return api


def test_worker_passes_raw_cal_callback(app_session_factory, admin_engine,
                                        clinic_a):
    dialog = SpyDialog(Reply("ок"))
    run_callback(app_session_factory, clinic_a, dialog, "cal:nav:2026-07")
    assert dialog.actions == ["cal:nav:2026-07"], "cal: идёт мимо tg_actions"


def test_worker_unknown_callback_still_stale(app_session_factory, admin_engine,
                                             clinic_a):
    dialog = SpyDialog(Reply("ок"))
    run_callback(app_session_factory, clinic_a, dialog, "a:99")
    assert dialog.actions == ["stale"]


def test_worker_edit_reply_edits_source_message(app_session_factory,
                                                admin_engine, clinic_a):
    dialog = SpyDialog(Reply("Июль 2026", edit=True,
                             button_rows=((Button("◀", "cal:nav:2026-06"),),)))
    api = run_callback(app_session_factory, clinic_a, dialog, "cal:nav:2026-07")

    assert api.edited, "сообщение отредактировано, не отправлено новое"
    chat_id, message_id, text_, rows = api.edited[-1]
    assert message_id == 77  # message_id из put_callback
    assert not api.sent, "нового сообщения нет"


def test_worker_toast_only_sends_nothing(app_session_factory, admin_engine,
                                         clinic_a):
    dialog = SpyDialog(Reply("", toast="Свободного времени нет"))
    api = run_callback(app_session_factory, clinic_a, dialog, "cal:noop")

    assert api.toasts == ["Свободного времени нет"]
    assert not api.sent and not api.edited
