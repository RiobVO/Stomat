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
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from navbat.db.base import tenant_transaction
from navbat.dialog.conversation import get_chat_lang
from navbat.dialog.escalation import (
    EscalationNotifier,
    LoggingEscalation,
    system_alert,
)
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.replies import Button, Reply, menu_rows, t
from navbat.telegram.api import ChatUnavailableError
from navbat.telegram.escalation import _as_chat_tuple
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
        admin_chat_id=None,  # int | список | None — авторизация админ-команд (M4)
    ) -> None:
        self._session_factory = session_factory
        self._clinic_id = clinic_id
        self._dialog = dialog
        self._api = api
        self._notifier = notifier or LoggingEscalation()
        self._admin_chat_ids = _as_chat_tuple(admin_chat_id)

    def process_one(self) -> bool:
        """Обрабатывает один апдейт; False — очередь пуста."""
        claimed = claim_next(self._session_factory, self._clinic_id)
        if claimed is None:
            return False
        try:
            self._handle(claimed)
        except ChatUnavailableError as e:
            # пациент заблокировал бота / удалил чат — ответ недоставим, но
            # это не сбой: гасим апдейт без ретраев и без эскалации (C2)
            log.info("апдейт %d: чат %s недоступен (%s) — гашу без эскалации",
                     claimed.update_id, claimed.tg_chat_id, e)
            with tenant_transaction(self._session_factory, self._clinic_id) as session:
                complete(session, claimed.id)
            return True
        except Exception as e:  # ack не выдаём — апдейт вернётся на повтор
            log.exception("апдейт %d: обработка упала", claimed.update_id)
            with tenant_transaction(self._session_factory, self._clinic_id) as session:
                status = fail(session, claimed.id)
            if status == "failed":
                system_alert(
                    self._notifier,
                    f"апдейт {claimed.update_id} в dead letter: {e}",
                    {"update_id": claimed.update_id},
                    chat_id=claimed.tg_chat_id,
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
                if self._bot_paused():
                    self._send(chat_id, self._paused_reply(chat_id))
                    return
                # телефон кнопкой request_contact; принимаем только собственный
                # контакт отправителя. Rate-limit не нужен: NLU не дёргается.
                # Номер уже хэширован на enqueue (открытым в очередь не попал);
                # phone_hash=None — не-узбекский номер, воркер эскалирует.
                contact = message["contact"]
                own = (contact.get("user_id") is not None
                       and contact["user_id"] == message.get("from", {}).get("id"))
                reply = self._dialog.handle_contact_hashed(
                    chat_id, contact.get("phone_hash"), own)
                self._send(chat_id, reply)
                return
            if "text" in message:
                if (message["text"].split()[:1] == ["/stats"]
                        and chat_id in self._admin_chat_ids):
                    self._send(chat_id, self._stats_reply(message["text"]))
                    return
                if (message["text"].split()[:1] == ["/release"]
                        and chat_id in self._admin_chat_ids):
                    self._send(chat_id, self._release_reply(message["text"]))
                    return
                if (message["text"].split()[:1] == ["/dayoff"]
                        and chat_id in self._admin_chat_ids):
                    self._send(chat_id, self._dayoff_reply(message["text"]))
                    return
                if (message["text"].split()[:1] == ["/dayopen"]
                        and chat_id in self._admin_chat_ids):
                    self._send(chat_id, self._dayopen_reply(message["text"]))
                    return
                if (message["text"].split()[:1] == ["/forget"]
                        and chat_id in self._admin_chat_ids):
                    self._send(chat_id, self._forget_reply(message["text"]))
                    return
                if (message["text"].split()[:1] == ["/pause"]
                        and chat_id in self._admin_chat_ids):
                    self._send(chat_id, self._pause_reply(message["text"]))
                    return
                if (message["text"].strip() == "/resume"
                        and chat_id in self._admin_chat_ids):
                    self._send(chat_id, self._resume_reply())
                    return
                if (message["text"].split()[:1] == ["/llm"]
                        and chat_id in self._admin_chat_ids):
                    self._send(chat_id, self._llm_reply(message["text"]))
                    return
                if self._bot_paused():
                    # пауза (/pause): вежливый ответ вместо диалога; команды
                    # админа выше по коду продолжают работать
                    self._send(chat_id, self._paused_reply(chat_id))
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
            if self._bot_paused():
                self._api.answer_callback_query(callback["id"])
                self._send(chat_id, self._paused_reply(chat_id))
                return
            data = callback.get("data", "")
            if data.startswith("cal:"):
                # сырой короткий callback календаря (П-4): идёт мимо
                # tg_actions-map — тот перезаписывается любой отправкой
                # кнопок (например, напоминанием), а календарь живёт долго
                action = data
            else:
                action = self._lookup_action(chat_id, data) or "stale"
            reply = self._dialog.handle_action(chat_id, action)
            self._api.answer_callback_query(callback["id"], text=reply.toast)
            message_id = callback["message"].get("message_id")
            if reply.edit and message_id is not None:
                edit_reply(self._api, self._session_factory, self._clinic_id,
                           chat_id, message_id, reply)
            elif reply.text:
                self._send(chat_id, reply)
            return
        log.info("служебный апдейт %d: пропущен", claimed.update_id)

    def _set_clinic_flag(self, column: str, value: bool) -> None:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            session.execute(text(
                f"UPDATE clinic SET {column} = :v "
                f"WHERE id = current_setting('app.clinic_id')::uuid"),
                {"v": value})

    def _pause_reply(self, command: str) -> Reply:
        """Пауза бота: /pause [причина] (C-4). Пациенты получают вежливое
        сообщение, напоминания продолжают ходить, /resume — обратно."""
        reason = command.partition(" ")[2].strip()
        self._set_clinic_flag("bot_paused", True)
        suffix = f" ({reason})" if reason else ""
        return Reply(f"[OK] бот на паузе{suffix}. Пациентам отвечаем "
                     f"«запись временно по телефону». Вернуть: /resume")

    def _resume_reply(self) -> Reply:
        self._set_clinic_flag("bot_paused", False)
        return Reply("[OK] бот снова принимает запись")

    def _llm_reply(self, command: str) -> Reply:
        """Рубильник NLU: /llm off — кнопки работают, свободный текст → меню."""
        arg = command.split()[1:2]
        if arg == ["off"]:
            self._set_clinic_flag("llm_enabled", False)
            return Reply("[OK] LLM выключен: кнопки работают, свободный "
                         "текст уходит в меню. Вернуть: /llm on")
        if arg == ["on"]:
            self._set_clinic_flag("llm_enabled", True)
            return Reply("[OK] LLM включён")
        return Reply("Формат: /llm on | /llm off")

    def _bot_paused(self) -> bool:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            return session.execute(text(
                "SELECT bot_paused FROM clinic "
                "WHERE id = current_setting('app.clinic_id')::uuid"
            )).scalar_one()

    def _paused_reply(self, chat_id: int) -> Reply:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            lang = get_chat_lang(session, chat_id)
        return Reply(t("bot_paused", lang))

    def _release_reply(self, command: str) -> Reply:
        """Снятие эскалации админом: /release <chat_id> (Ф1.5, BRIEF разд. 14.A).

        Conversation → idle, счётчик сбоев NLU в ноль; пациенту уходит главное
        меню — он должен видеть, что бот снова отвечает.
        """
        parts = command.split()
        if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
            return Reply("Формат: /release <chat_id> (число из алерта эскалации)")
        target = int(parts[1])
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            row = session.execute(
                text("SELECT fsm_state, context ->> 'lang' AS lang "
                     "FROM conversation WHERE tg_chat_id = :chat"),
                {"chat": target},
            ).one_or_none()
            if row is None:
                return Reply(f"Чат {target} не найден.")
            if row.fsm_state != "escalated":
                return Reply(f"Чат {target} не в эскалации "
                             f"(состояние: {row.fsm_state}).")
            session.execute(
                text("UPDATE conversation SET fsm_state = 'idle', "
                     "context = jsonb_set(context, '{nlu_failures}', '0', true) "
                     "WHERE tg_chat_id = :chat"),
                {"chat": target},
            )
        lang = row.lang or "ru"
        self._send(target, Reply(t("menu_hint", lang), menu=menu_rows(lang)))
        return Reply(f"[OK] эскалация снята: чат {target}")

    def _forget_reply(self, command: str) -> Reply:
        """Анонимизация пациента по запросу: /forget <chat_id> (Ф1.5, D.2).

        Имя/контакт стираются, диалог и сырые сообщения удаляются; история
        приёмов остаётся обезличенной (appointment.tg_chat_id → NULL).
        Будущие записи НЕ отменяются: запрос на удаление данных — не отмена
        приёма; pending-напоминания гасятся, чтобы не слать в стёртый чат.
        """
        parts = command.split()
        if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
            return Reply("Формат: /forget <chat_id> — анонимизировать пациента")
        target = int(parts[1])
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            reminders = session.execute(
                text("UPDATE reminder SET status = 'cancelled' "
                     "WHERE status = 'pending' AND appointment_id IN "
                     "(SELECT id FROM appointment WHERE tg_chat_id = :chat)"),
                {"chat": target}).rowcount
            patients = session.execute(
                text("UPDATE patient SET name_encrypted = NULL, "
                     "contact_hash = NULL, tg_chat_id = NULL "
                     "WHERE tg_chat_id = :chat"), {"chat": target}).rowcount
            appointments = session.execute(
                text("UPDATE appointment SET tg_chat_id = NULL "
                     "WHERE tg_chat_id = :chat"), {"chat": target}).rowcount
            dialogs = session.execute(
                text("DELETE FROM conversation WHERE tg_chat_id = :chat"),
                {"chat": target}).rowcount
            messages = session.execute(
                text("DELETE FROM message_queue WHERE tg_chat_id = :chat"),
                {"chat": target}).rowcount
        if not any((reminders, patients, appointments, dialogs, messages)):
            return Reply(f"Чат {target} не найден — данных нет.")
        return Reply(
            f"[OK] чат {target}: пациент анонимизирован, диалог и сообщения "
            f"удалены. Будущие записи не отменены — отмените отдельно, "
            f"если пациент просил.")

    # ── Выходные дни: клиника сама закрывает/открывает (Ф1.5) ────────────

    def _clinic_today(self) -> date:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            tz = ZoneInfo(session.execute(
                text("SELECT timezone FROM clinic "
                     "WHERE id = current_setting('app.clinic_id')::uuid")
            ).scalar_one())
        return datetime.now(tz).date()

    def _dayoff_reply(self, command: str) -> Reply:
        """Закрыть день: /dayoff DD.MM [причина].

        Предзаполненного календаря праздников нет (решение 06.06.2026):
        кому нужен выходной — сам закрывает день из админ-чата. Закрытый
        день уважают и слоты, и «сейчас закрыто» (таблица holiday).
        """
        parts = command.split(maxsplit=2)
        today = self._clinic_today()
        target = self._parse_ddmm(parts[1], today) if len(parts) > 1 else None
        if target is None:
            return Reply(self._dayoff_usage(today))
        reason = parts[2].strip() if len(parts) > 2 else None
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            exists = session.execute(
                text("SELECT 1 FROM holiday WHERE date = :d"), {"d": target}
            ).scalar_one_or_none()
            if exists:
                return Reply(f"{target:%d.%m.%Y} уже выходной.")
            session.execute(
                text("INSERT INTO holiday (clinic_id, date, reason) VALUES "
                     "(current_setting('app.clinic_id')::uuid, :d, :r)"),
                {"d": target, "r": reason},
            )
        label = f" ({reason})" if reason else ""
        return Reply(f"[OK] {target:%d.%m.%Y} — выходной{label}")

    def _dayopen_reply(self, command: str) -> Reply:
        """Снова открыть закрытый день: /dayopen DD.MM."""
        parts = command.split()
        today = self._clinic_today()
        target = self._parse_ddmm(parts[1], today) if len(parts) == 2 else None
        if target is None:
            return Reply(self._dayoff_usage(today))
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            deleted = session.execute(
                text("DELETE FROM holiday WHERE date = :d RETURNING id"),
                {"d": target},
            ).first()
        if deleted is None:
            return Reply(f"{target:%d.%m.%Y} и так рабочий.")
        return Reply(f"[OK] {target:%d.%m.%Y} снова рабочий")

    @staticmethod
    def _parse_ddmm(raw: str, today: date) -> date | None:
        """«21.03» → ближайшая (включая сегодня) будущая дата с таким днём/месяцем."""
        day_raw, _, month_raw = raw.partition(".")
        try:
            day, month = int(day_raw), int(month_raw)
        except ValueError:
            return None
        for year in range(today.year, today.year + 5):  # 29.02 ждёт високосного
            try:
                candidate = date(year, month, day)
            except ValueError:
                continue
            if candidate >= today:
                return candidate
        return None  # 31.02 и прочие несуществующие даты

    def _dayoff_usage(self, today: date) -> str:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            rows = session.execute(
                text("SELECT date, reason FROM holiday WHERE date >= :t "
                     "ORDER BY date LIMIT 5"),
                {"t": today},
            ).all()
        if rows:
            days = "; ".join(
                f"{row.date:%d.%m.%Y}" + (f" ({row.reason})" if row.reason else "")
                for row in rows)
            upcoming = f"Ближайшие выходные: {days}"
        else:
            upcoming = "Закрытых дней впереди нет."
        return ("Формат: /dayoff DD.MM [причина] — закрыть день, "
                "/dayopen DD.MM — снова открыть.\n" + upcoming)

    def _stats_reply(self, command: str = "/stats") -> Reply:
        """Сводка владельца (П-6): /stats — день, /stats 7|30 — период."""
        from navbat.stats import collect_stats, render_stats

        args = command.split()[1:2]
        days = 1
        if args:
            if not args[0].isdigit() or not 1 <= int(args[0]) <= 90:
                return Reply("Формат: /stats [дней], например /stats 7 "
                             "или /stats 30")
            days = int(args[0])
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            tz = ZoneInfo(session.execute(
                text("SELECT timezone FROM clinic "
                     "WHERE id = current_setting('app.clinic_id')::uuid")
            ).scalar_one())
            today = datetime.now(tz).date()
            first = today - timedelta(days=days - 1)
            stats = collect_stats(session, first, today, tz)
        return Reply(render_stats(stats, first, today))

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


def _number_buttons(session_factory: sessionmaker[Session], clinic_id: uuid.UUID,
                    chat_id: int,
                    rows: tuple[tuple[Button, ...], ...]
                    ) -> tuple[tuple[Button, ...], ...]:
    """Длинные action'ы → a:N + map в context['tg_actions']; короткие
    cal:-действия (П-4) уходят сырыми — переживают перезапись map'а."""
    mapping: dict[str, str] = {}
    out_rows: list[tuple[Button, ...]] = []
    for row in rows:
        out_row = []
        for b in row:
            if b.action.startswith("cal:"):
                out_row.append(b)
            else:
                index = str(len(mapping) + 1)
                mapping[index] = b.action
                out_row.append(Button(b.label, f"a:{index}"))
        out_rows.append(tuple(out_row))
    if mapping:
        with tenant_transaction(session_factory, clinic_id) as session:
            session.execute(
                text("UPDATE conversation "
                     "SET context = jsonb_set(context, '{tg_actions}', "
                     "                        CAST(:mapping AS jsonb), true) "
                     "WHERE tg_chat_id = :chat"),
                {"mapping": json.dumps(mapping, ensure_ascii=False), "chat": chat_id},
            )
    return tuple(out_rows)


def send_reply(api, session_factory: sessionmaker[Session], clinic_id: uuid.UUID,
               chat_id: int, reply: Reply) -> None:
    """Отправка Reply в чат: кнопки нумеруются, map уходит в context.

    Используется воркером и календарным sync'ом (уведомления о переносе).
    """
    if reply.button_rows:
        rows = _number_buttons(session_factory, clinic_id, chat_id,
                               reply.button_rows)
        api.send_message(chat_id, reply.text, button_rows=rows,
                         contact_request=reply.contact_request, menu=reply.menu)
        return
    buttons = reply.buttons
    if buttons:
        rows = _number_buttons(session_factory, clinic_id, chat_id,
                               tuple((b,) for b in buttons))
        buttons = tuple(row[0] for row in rows)
    api.send_message(chat_id, reply.text, buttons,
                     contact_request=reply.contact_request,
                     menu=reply.menu)


def edit_reply(api, session_factory: sessionmaker[Session], clinic_id: uuid.UUID,
               chat_id: int, message_id: int, reply: Reply) -> None:
    """Редактирование сообщения-источника callback'а (П-4): календарь
    листается на месте, без спама в чат. Нумерация — та же, что в send."""
    rows = _number_buttons(session_factory, clinic_id, chat_id,
                           reply.button_rows or tuple((b,) for b in reply.buttons))
    api.edit_message_text(chat_id, message_id, reply.text, button_rows=rows)
