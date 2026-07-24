"""Воркер: апдейт из очереди → FSM → ответ в Telegram.

Ack (done) — только после успешной отправки ответа: упавший send вернёт
апдейт в pending, 3 неудачи — dead letter + эскалация админу.

callback_data ограничен 64 байтами, а action-строки FSM длиннее — кнопки
ответа нумеруются, полный map уходит в conversation.context["tg_actions"],
в Telegram летит «a:<N>». Кнопка из устаревшего сообщения (map уже
перезаписан) превращается в action «stale» — FSM отвечает повтором шага.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from navbat.db.base import tenant_transaction
from navbat.dialog.conversation import get_chat_lang
from navbat.dialog.escalation import EscalationNotifier, LoggingEscalation
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.replies import Button, Reply, t
from navbat.telegram.queue import (
    QueuedUpdate,
    claim_next,
    complete,
    fail,
    reclaim_stale,
)

log = logging.getLogger("navbat.telegram")

IDLE_WAIT = 0.3        # сек между опросами пустой очереди
RECLAIM_EVERY = 60.0   # сек между реклеймами зависших processing
RATE_MAX = 5           # сообщений на чат за окно — дальше NLU не дёргаем
RATE_WINDOW_SECONDS = 10


class UpdateWorker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clinic_id: uuid.UUID,
        dialog: DialogEngine,
        api,  # TelegramAPI | FakeTelegramAPI (duck typing для тестов)
        notifier: EscalationNotifier | None = None,
        admin_chat_id: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clinic_id = clinic_id
        self._dialog = dialog
        self._api = api
        self._notifier = notifier or LoggingEscalation()
        self._admin_chat_id = admin_chat_id

    def process_one(self) -> bool:
        """Обрабатывает один апдейт; False — очередь пуста."""
        claimed = claim_next(self._session_factory, self._clinic_id)
        if claimed is None:
            return False
        try:
            self._handle(claimed)
        except Exception as e:  # ack не выдаём — апдейт вернётся на повтор
            log.exception("апдейт %d: обработка упала", claimed.update_id)
            with tenant_transaction(self._session_factory, self._clinic_id) as session:
                status = fail(session, claimed.id)
            if status == "failed":
                self._notifier.notify(
                    claimed.tg_chat_id,
                    f"апдейт {claimed.update_id} в dead letter: {e}",
                    {"update_id": claimed.update_id},
                )
            return True
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            complete(session, claimed.id)
        return True

    def run(self, stop: threading.Event) -> None:
        """Цикл воркера до сигнала остановки."""
        last_reclaim = time.monotonic()
        while not stop.is_set():
            if time.monotonic() - last_reclaim >= RECLAIM_EVERY:
                with tenant_transaction(self._session_factory, self._clinic_id) as session:
                    reclaimed = reclaim_stale(session)
                if reclaimed:
                    log.warning("возвращено зависших апдейтов: %d", reclaimed)
                last_reclaim = time.monotonic()
            if not self.process_one():
                stop.wait(IDLE_WAIT)

    # ── Разбор апдейта ───────────────────────────────────────────────────

    def _handle(self, claimed: QueuedUpdate) -> None:
        payload = claimed.payload
        if "message" in payload:
            message = payload["message"]
            chat_id = message["chat"]["id"]
            if "contact" in message:
                # телефон кнопкой request_contact; принимаем только собственный
                # контакт отправителя. Rate-limit не нужен: NLU не дёргается.
                contact = message["contact"]
                own = (contact.get("user_id") is not None
                       and contact["user_id"] == message.get("from", {}).get("id"))
                reply = self._dialog.handle_contact(
                    chat_id, contact.get("phone_number", ""), own)
                self._send(chat_id, reply)
                return
            if "text" in message:
                if (message["text"].strip() == "/stats"
                        and chat_id == self._admin_chat_id):
                    self._send(chat_id, self._stats_reply())
                    return
                verdict = self._rate_verdict(chat_id, claimed.id)
                if verdict == "silent":
                    return
                if verdict == "warn":
                    with tenant_transaction(self._session_factory,
                                            self._clinic_id) as session:
                        lang = get_chat_lang(session, chat_id)
                    self._send(chat_id, Reply(t("rate_limited", lang)))
                    return
                reply = self._dialog.handle_text(chat_id, message["text"])
            else:
                # фото/стикер/голос: язык чата ещё может быть неизвестен — обе строки
                reply = Reply(f"{t('text_only', 'ru')}\n{t('text_only', 'uz')}")
            self._send(chat_id, reply)
            return
        if "callback_query" in payload:
            callback = payload["callback_query"]
            chat_id = callback["message"]["chat"]["id"]
            action = self._lookup_action(chat_id, callback.get("data", ""))
            reply = self._dialog.handle_action(chat_id, action or "stale")
            self._api.answer_callback_query(callback["id"])
            self._send(chat_id, reply)
            return
        log.info("служебный апдейт %d: пропущен", claimed.update_id)

    def _stats_reply(self) -> Reply:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from navbat.stats import collect_daily_stats, render_stats

        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            tz = ZoneInfo(session.execute(
                text("SELECT timezone FROM clinic "
                     "WHERE id = current_setting('app.clinic_id')::uuid")
            ).scalar_one())
            today = datetime.now(tz).date()
            stats = collect_daily_stats(session, today, tz)
        return Reply(render_stats(stats, today))

    def _rate_verdict(self, chat_id: int, current_queue_id: int) -> str:
        """ok | warn | silent: защита кошелька от залпа сообщений (BRIEF).

        Считаются сообщения, принятые ДО текущего: нормальный пациент
        с 2–3 сообщениями подряд лимита не чувствует.
        """
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            previous = session.execute(
                text("SELECT count(*) FROM message_queue "
                     "WHERE tg_chat_id = :chat AND id < :current "
                     "AND created_at > now() - make_interval(secs => :window)"),
                {"chat": chat_id, "current": current_queue_id,
                 "window": RATE_WINDOW_SECONDS},
            ).scalar_one()
        if previous < RATE_MAX:
            return "ok"
        # предупреждаем один раз (ровно на превышении), дальше молчим
        return "warn" if previous == RATE_MAX else "silent"

    # ── Кнопки: короткий callback_data + map в контексте ─────────────────

    def _send(self, chat_id: int, reply: Reply) -> None:
        send_reply(self._api, self._session_factory, self._clinic_id, chat_id, reply)

    def _lookup_action(self, chat_id: int, data: str) -> str | None:
        if not data.startswith("a:"):
            return None
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            return session.execute(
                text("SELECT context #>> ARRAY['tg_actions', :index] "
                     "FROM conversation WHERE tg_chat_id = :chat"),
                {"index": data[2:], "chat": chat_id},
            ).scalar_one_or_none()


def send_reply(api, session_factory: sessionmaker[Session], clinic_id: uuid.UUID,
               chat_id: int, reply: Reply) -> None:
    """Отправка Reply в чат: кнопки нумеруются, map уходит в context.

    Используется воркером и календарным sync'ом (уведомления о переносе).
    """
    buttons = reply.buttons
    if buttons:
        mapping = {str(i): b.action for i, b in enumerate(buttons, 1)}
        with tenant_transaction(session_factory, clinic_id) as session:
            session.execute(
                text("UPDATE conversation "
                     "SET context = jsonb_set(context, '{tg_actions}', "
                     "                        CAST(:mapping AS jsonb), true) "
                     "WHERE tg_chat_id = :chat"),
                {"mapping": json.dumps(mapping, ensure_ascii=False), "chat": chat_id},
            )
        buttons = tuple(Button(b.label, f"a:{i}") for i, b in enumerate(buttons, 1))
    api.send_message(chat_id, reply.text, buttons,
                     contact_request=reply.contact_request,
                     remove_keyboard=reply.remove_keyboard)
