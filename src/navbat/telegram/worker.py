"""Воркер: апдейт из очереди → FSM → ответ в Telegram.

Ack (done) — только после успешной отправки ответа: упавший send вернёт
апдейт в pending, 3 неудачи — dead letter + эскалация админу.

callback_data ограничен 64 байтами, а action-строки FSM длиннее — кнопки
ответа нумеруются, полный map уходит в conversation.context["tg_actions"],
в Telegram летит «a:<N>». Кнопка из устаревшего сообщения (map уже
перезаписан) превращается в action «stale» — FSM отвечает повтором шага.
"""
from __future__ import annotations

import html
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
from navbat.dialog import clinic_repo
from navbat.dialog.conversation import get_chat_lang
from navbat.dialog.escalation import (
    EscalationNotifier,
    LoggingEscalation,
    ops_alert,
    system_alert,
)
from navbat.dialog.fsm import DialogEngine
from navbat.dialog.replies import Button, Reply, menu_rows, t
from navbat.telegram.admin_console import AdminConsole, booked_warning
from navbat.telegram.admin_texts import Reason, at
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
ERROR_BACKOFF = 2.0    # сек паузы после сбоя цикла — не долбить мёртвую БД
RATE_MAX = 5           # сообщений на чат за окно — дальше NLU не дёргаем
RATE_WINDOW_SECONDS = 10

# Пациентские кнопки, которые живут дольше одной отправки, поэтому несут своё
# действие сами и не занимают номер в tg_actions-map: карта одна на чат и
# перезаписывается КАЖДОЙ отправкой кнопок. Календарь и лист ожидания (П-4,
# К-1) приходят часами позже, кнопка выхода из заморозки висит, пока пациент
# ждёт администратора, а напоминание живёт до самого приёма — у пациента может
# быть несколько записей, и пронумерованная кнопка под ранним напоминанием
# указала бы на запись из последнего пуша. reslot: — альтернативы вытесненной
# записи из календарного синка: тот же долгий срок жизни.
RAW_CALLBACK_PREFIXES = ("cal:", "wl:", "unfreeze", "attend:", "remind_cancel:",
                         "reslot:", "time:", "d:")
# Плюс админские префиксы: их роутинг — отдельные ветки в _handle (до паузы),
# в пациентскую карту кнопок они попадать не должны тем более.
UNNUMBERED_PREFIXES = RAW_CALLBACK_PREFIXES + ("stats:", "adm:")


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
        self._admin = AdminConsole(session_factory, clinic_id, api, self)

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
                ops_alert(
                    self._notifier,
                    Reason("reason_update_failed",
                           chat=claimed.tg_chat_id),
                    {"update_id": claimed.update_id},
                    chat_id=claimed.tg_chat_id,
                    detail=f"апдейт {claimed.update_id} в dead letter: {e}",
                )
            return True
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            complete(session, claimed.id)
        return True

    def run(self, stop: threading.Event) -> None:
        """Цикл воркера до сигнала остановки.

        Клейм, ack и реклейм ходят в БД вне защиты process_one, поэтому
        обрыв соединения (перезапуск Postgres, OOM) уносил бы весь поток:
        процесс остался бы жив, транспорт продолжал бы копить апдейты, а
        пациенты не получали бы ответов молча. Цикл переживает сбой и ждёт
        восстановления — как цикл напоминаний.
        """
        last_reclaim = time.monotonic()
        while not stop.is_set():
            try:
                if time.monotonic() - last_reclaim >= RECLAIM_EVERY:
                    with tenant_transaction(self._session_factory,
                                            self._clinic_id) as session:
                        reclaimed = reclaim_stale(session)
                    if reclaimed:
                        log.warning("возвращено зависших апдейтов: %d", reclaimed)
                    last_reclaim = time.monotonic()
                if not self.process_one():
                    stop.wait(IDLE_WAIT)
            except Exception:
                log.exception("цикл воркера упал — продолжаю")
                stop.wait(ERROR_BACKOFF)

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
                # Номер уже хэширован и зашифрован на enqueue (открытым
                # в очередь не попал); phone_hash=None — номер не распознан,
                # диалог повторит кнопку.
                contact = message["contact"]
                own = (contact.get("user_id") is not None
                       and contact["user_id"] == message.get("from", {}).get("id"))
                reply = self._dialog.handle_contact_hashed(
                    chat_id, contact.get("phone_hash"),
                    contact.get("phone_encrypted"), own)
                self._send(chat_id, reply)
                return
            if "text" in message:
                if (message["text"].split()[:1] == ["/stats"]
                        and chat_id in self._admin_chat_ids):
                    self._send(chat_id,
                               self._stats_reply(message["text"],
                                                 self._admin_lang(chat_id)))
                    return
                if (message["text"].split()[:1] == ["/release"]
                        and chat_id in self._admin_chat_ids):
                    self._send(chat_id,
                               self._release_reply(message["text"],
                                                   self._admin_lang(chat_id)))
                    return
                if (message["text"].split()[:1] == ["/dayoff"]
                        and chat_id in self._admin_chat_ids):
                    self._send(chat_id,
                               self._dayoff_reply(message["text"],
                                                  self._admin_lang(chat_id)))
                    return
                if (message["text"].split()[:1] == ["/dayopen"]
                        and chat_id in self._admin_chat_ids):
                    self._send(chat_id,
                               self._dayopen_reply(message["text"],
                                                   self._admin_lang(chat_id)))
                    return
                if (message["text"].split()[:1] == ["/forget"]
                        and chat_id in self._admin_chat_ids):
                    self._send(chat_id,
                               self._forget_reply(message["text"],
                                                  self._admin_lang(chat_id)))
                    return
                if (message["text"].split()[:1] == ["/pause"]
                        and chat_id in self._admin_chat_ids):
                    self._send(chat_id,
                               self._pause_reply(message["text"],
                                                 self._admin_lang(chat_id)))
                    return
                if (message["text"].strip() == "/resume"
                        and chat_id in self._admin_chat_ids):
                    self._send(chat_id, self._resume_reply(self._admin_lang(chat_id)))
                    return
                if (message["text"].split()[:1] == ["/llm"]
                        and chat_id in self._admin_chat_ids):
                    self._send(chat_id,
                               self._llm_reply(message["text"],
                                               self._admin_lang(chat_id)))
                    return
                if chat_id in self._admin_chat_ids:
                    # админ-чат = чистая консоль: владелец НИКОГДА не попадает
                    # в пациентский диалог. Перехват ДО паузы (как /stats) —
                    # консоль живёт и при /pause (C-4). Слэш-команды выше по
                    # коду остаются аварийным выходом даже посреди ввода.
                    self._send(chat_id,
                               self._admin.handle_text(chat_id, message["text"]))
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
            data = callback.get("data", "")
            if data.startswith("stats:") and chat_id in self._admin_chat_ids:
                # кнопки сводки (В) до проверки паузы: админ-поверхность
                # живёт и при /pause (конвенция C-4)
                self._handle_stats_callback(callback, chat_id, data)
                return
            if data.startswith("adm:") and chat_id in self._admin_chat_ids:
                # админ-консоль (сырой префикс, мимо tg_actions-map); до паузы
                self._admin.handle_callback(callback, chat_id, data)
                return
            if chat_id in self._admin_chat_ids:
                # админ-чат = чистая консоль и для callback'ов: нераспознанная
                # (например, старая пациентская a:N) кнопка НЕ уходит в
                # пациентский диалог — подтверждаем и показываем админ-меню
                self._api.answer_callback_query(callback["id"])
                self._send(chat_id, self._admin.main_menu(chat_id))
                return
            if self._bot_paused():
                self._api.answer_callback_query(callback["id"])
                self._send(chat_id, self._paused_reply(chat_id))
                return
            if data.startswith(RAW_CALLBACK_PREFIXES):
                # сырые короткие callback'и: календарь (П-4), лист ожидания,
                # выход из заморозки, кнопки напоминания и альтернативы
                # вытесненной записи — идут мимо tg_actions-map (тот
                # перезаписывается любой отправкой кнопок); wl:-пуш приходит
                # часами позже отмены, кнопка выхода висит, пока пациент ждёт
                # администратора, напоминание живёт до самого приёма, а
                # reslot: приходит из календарного синка и висит столько же —
                # любой следующий пуш занял бы их номера
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

    def _admin_lang(self, chat_id: int) -> str:
        """Язык админ-чата (карта, №16): ответы команд идут на нём же."""
        return self._admin._lang(chat_id)

    def _pause_reply(self, command: str, lang: str = "ru") -> Reply:
        """Пауза бота: /pause [причина] (C-4). Пациенты получают вежливое
        сообщение, напоминания продолжают ходить, /resume — обратно."""
        # причина — текст админа: at() её экранирует, ответ уходит с HTML (П-7)
        reason = command.partition(" ")[2].strip()
        self._set_clinic_flag("bot_paused", True)
        key = "paused_ok_reason" if reason else "paused_ok"
        return Reply(at(key, lang, reason=reason) if reason
                     else at(key, lang))

    def _resume_reply(self, lang: str = "ru") -> Reply:
        self._set_clinic_flag("bot_paused", False)
        return Reply(at("resumed_ok", lang))

    def _llm_reply(self, command: str, lang: str = "ru") -> Reply:
        """Рубильник NLU: /llm off — кнопки работают, свободный текст → меню."""
        arg = command.split()[1:2]
        if arg == ["off"]:
            self._set_clinic_flag("llm_enabled", False)
            return Reply(at("llm_off_ok", lang))
        if arg == ["on"]:
            self._set_clinic_flag("llm_enabled", True)
            return Reply(at("llm_on_ok", lang))
        return Reply(at("llm_usage", lang))

    def _bot_paused(self) -> bool:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            return session.execute(text(
                "SELECT bot_paused FROM clinic "
                "WHERE id = current_setting('app.clinic_id')::uuid"
            )).scalar_one()

    def _paused_reply(self, chat_id: int) -> Reply:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            lang = get_chat_lang(session, chat_id)
            # на паузе телефон — единственный оставшийся у пациента путь;
            # не задан (онбординг без --phone) — прежний текст без номера
            phone = clinic_repo.clinic_phone(session)
        if phone:
            return Reply(t("bot_paused_phone", lang, phone=phone))
        return Reply(t("bot_paused", lang))

    def _release_reply(self, command: str, lang: str = "ru") -> Reply:
        """Снятие эскалации админом: /release <chat_id> (Ф1.5, BRIEF разд. 14.A).

        Conversation → idle, счётчик сбоев NLU в ноль; пациенту уходит главное
        меню — он должен видеть, что бот снова отвечает.
        """
        parts = command.split()
        if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
            return Reply(at("release_usage", lang))
        target = int(parts[1])
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            row = session.execute(
                text("SELECT fsm_state, context ->> 'lang' AS lang "
                     "FROM conversation WHERE tg_chat_id = :chat"),
                {"chat": target},
            ).one_or_none()
            if row is None:
                return Reply(at("release_not_found", lang, chat=target))
            if row.fsm_state != "escalated":
                return Reply(at("release_not_escalated", lang, chat=target,
                                state=row.fsm_state))
            # кулдаун повторных алертов тоже в ноль: админ вернул бота —
            # новая просьба человека снова важна
            session.execute(
                text("UPDATE conversation SET fsm_state = 'idle', "
                     "context = jsonb_set(context, '{nlu_failures}', '0', true) "
                     "- 'escalation_alerted_at' "
                     "WHERE tg_chat_id = :chat"),
                {"chat": target},
            )
        # у пациента свой язык, у администратора свой: меню уходит на
        # пациентском, подтверждение — на языке админ-чата
        patient_lang = row.lang or "ru"
        self._send(target, Reply(t("menu_hint", patient_lang),
                                 menu=menu_rows(patient_lang)))
        return Reply(at("release_ok", lang, chat=target))

    def _forget_reply(self, command: str, lang: str = "ru") -> Reply:
        """Анонимизация пациента по запросу: /forget <chat_id> (Ф1.5, D.2).

        Имя/контакт стираются, диалог и сырые сообщения удаляются; история
        приёмов остаётся обезличенной (appointment.tg_chat_id → NULL).
        Будущие записи НЕ отменяются: запрос на удаление данных — не отмена
        приёма; pending-напоминания гасятся, чтобы не слать в стёртый чат,
        по той же причине снимается очередь ожидания (её строка хранит
        tg_chat_id, а матчер пишет в чат каждые 30 секунд).
        """
        parts = command.split()
        if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
            return Reply(at("forget_usage", lang))
        target = int(parts[1])
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            reminders = session.execute(
                text("UPDATE reminder SET status = 'cancelled' "
                     "WHERE status = 'pending' AND appointment_id IN "
                     "(SELECT id FROM appointment WHERE tg_chat_id = :chat)"),
                {"chat": target}).rowcount
            patients = session.execute(
                text("UPDATE patient SET name_encrypted = NULL, "
                     "contact_hash = NULL, phone_encrypted = NULL, "
                     "tg_chat_id = NULL "
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
            waiting = session.execute(
                text("DELETE FROM waitlist WHERE tg_chat_id = :chat"),
                {"chat": target}).rowcount
        if not any((reminders, patients, appointments, dialogs, messages,
                    waiting)):
            return Reply(at("forget_not_found", lang, chat=target))
        return Reply(at("forget_ok", lang, chat=target))

    # ── Выходные дни: клиника сама закрывает/открывает (Ф1.5) ────────────

    def _clinic_today(self) -> date:
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            tz = ZoneInfo(session.execute(
                text("SELECT timezone FROM clinic "
                     "WHERE id = current_setting('app.clinic_id')::uuid")
            ).scalar_one())
        return datetime.now(tz).date()

    def _dayoff_reply(self, command: str, lang: str = "ru") -> Reply:
        """Закрыть день: /dayoff DD.MM [причина].

        Предзаполненного календаря праздников нет (решение 06.06.2026):
        кому нужен выходной — сам закрывает день из админ-чата. Закрытый
        день уважают и слоты, и «сейчас закрыто» (таблица holiday).
        """
        parts = command.split(maxsplit=2)
        today = self._clinic_today()
        target = self._parse_ddmm(parts[1], today) if len(parts) > 1 else None
        if target is None:
            return Reply(self._dayoff_usage(today, lang))
        reason = parts[2].strip() if len(parts) > 2 else None
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            exists = session.execute(
                text("SELECT 1 FROM holiday WHERE date = :d"), {"d": target}
            ).scalar_one_or_none()
            if exists:
                return Reply(at("dayoff_already", lang,
                                date=f"{target:%d.%m.%Y}"))
            session.execute(
                text("INSERT INTO holiday (clinic_id, date, reason) VALUES "
                     "(current_setting('app.clinic_id')::uuid, :d, :r)"),
                {"d": target, "r": reason},
            )
            # закрытие дня записи не отменяет (решение владельца) — но владелец
            # обязан узнать о них здесь, а не от пришедшего пациента (карта, №7)
            warning = booked_warning(session, target, lang)
        # причина — текст админа: at() её экранирует, ответ уходит с HTML (П-7)
        label = at("reason_suffix", lang, reason=reason) if reason else ""
        return Reply(at("dayoff_ok", lang, date=f"{target:%d.%m.%Y}")
                     + label + warning)

    def _dayopen_reply(self, command: str, lang: str = "ru") -> Reply:
        """Снова открыть закрытый день: /dayopen DD.MM."""
        parts = command.split()
        today = self._clinic_today()
        target = self._parse_ddmm(parts[1], today) if len(parts) == 2 else None
        if target is None:
            return Reply(self._dayoff_usage(today, lang))
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            deleted = session.execute(
                text("DELETE FROM holiday WHERE date = :d RETURNING id"),
                {"d": target},
            ).first()
        if deleted is None:
            return Reply(at("dayopen_already", lang,
                            date=f"{target:%d.%m.%Y}"))
        return Reply(at("dayopen_ok", lang, date=f"{target:%d.%m.%Y}"))

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

    def _dayoff_usage(self, today: date, lang: str = "ru") -> str:
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
            upcoming = at("dayoff_upcoming", lang, days=days)
        else:
            upcoming = at("dayoff_none_ahead", lang)
        # склейка, а не подстановка: at() внутри at() экранировал бы
        # причину дважды и показывал «&lt;ремонт&gt;» (ревью, дефект 5)
        return at("dayoff_usage", lang) + "\n" + upcoming

    def _stats_reply(self, command: str = "/stats", lang: str = "ru") -> Reply:
        """Сводка владельца (П-6): /stats — день, /stats 7|30 — период."""
        args = command.split()[1:2]
        days = 1
        if args:
            if not args[0].isdigit() or not 1 <= int(args[0]) <= 90:
                return Reply(at("stats_usage", lang))
            days = int(args[0])
        return self._stats_view(days, lang)

    def _stats_view(self, days: int, lang: str = "ru") -> Reply:
        from navbat.stats import collect_stats, render_stats

        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            tz = ZoneInfo(session.execute(
                text("SELECT timezone FROM clinic "
                     "WHERE id = current_setting('app.clinic_id')::uuid")
            ).scalar_one())
            today = datetime.now(tz).date()
            first = today - timedelta(days=days - 1)
            stats = collect_stats(session, first, today, tz)
            # тренды (В): окно того же размера непосредственно перед текущим;
            # второй collect дёшев — объёмы одной клиники малы
            prev = collect_stats(session, first - timedelta(days=days),
                                 first - timedelta(days=1), tz)
        return Reply(render_stats(stats, first, today, prev=prev, lang=lang),
                     button_rows=(_period_buttons(days, lang),))

    def _handle_stats_callback(self, callback: dict, chat_id: int,
                               data: str) -> None:
        """Кнопки сводки (В): stats:N — edit на месте, stats:full — полная
        сводка дня НОВЫМ сообщением (дайджест с вопросами остаётся в чате)."""
        self._api.answer_callback_query(callback["id"])
        suffix = data[len("stats:"):]
        lang = self._admin_lang(chat_id)
        if suffix == "full":
            self._send(chat_id, self._stats_reply(lang=lang))
            return
        message_id = callback["message"].get("message_id")
        if suffix.isdigit() and 1 <= int(suffix) <= 90 and message_id is not None:
            edit_reply(self._api, self._session_factory, self._clinic_id,
                       chat_id, message_id,
                       self._stats_view(int(suffix), lang))

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

    def _edit(self, chat_id: int, message_id: int, reply: Reply) -> None:
        edit_reply(self._api, self._session_factory, self._clinic_id,
                   chat_id, message_id, reply)

    def _lookup_action(self, chat_id: int, data: str) -> str | None:
        if not data.startswith("a:"):
            return None
        with tenant_transaction(self._session_factory, self._clinic_id) as session:
            return session.execute(
                text("SELECT context #>> ARRAY['tg_actions', :index] "
                     "FROM conversation WHERE tg_chat_id = :chat"),
                {"index": data[2:], "chat": chat_id},
            ).scalar_one_or_none()


def _period_buttons(active: int, lang: str = "ru") -> tuple[Button, ...]:
    """Ряд периодов сводки (В): активный помечен ✓; callback'и сырые stats:N —
    мимо tg_actions, переживают перезапись map'а (как cal: в П-4)."""
    labels = ((1, at("btn_period_today", lang)),
              (7, at("btn_period", lang, days=7)),
              (30, at("btn_period", lang, days=30)))
    return tuple(
        Button(f"{label} ✓" if days == active else label, f"stats:{days}")
        for days, label in labels)


def _number_buttons(session_factory: sessionmaker[Session], clinic_id: uuid.UUID,
                    chat_id: int,
                    rows: tuple[tuple[Button, ...], ...]
                    ) -> tuple[tuple[Button, ...], ...]:
    """Длинные action'ы → a:N + map в context['tg_actions']; короткие
    cal:- (П-4) и stats:-действия (В) уходят сырыми — переживают
    перезапись map'а."""
    mapping: dict[str, str] = {}
    out_rows: list[tuple[Button, ...]] = []
    for row in rows:
        out_row = []
        for b in row:
            if b.action.startswith(UNNUMBERED_PREFIXES):
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
                         contact_request=reply.contact_request, menu=reply.menu,
                         parse_mode="HTML")
        return
    buttons = reply.buttons
    if buttons:
        rows = _number_buttons(session_factory, clinic_id, chat_id,
                               tuple((b,) for b in buttons))
        buttons = tuple(row[0] for row in rows)
    api.send_message(chat_id, reply.text, buttons,
                     contact_request=reply.contact_request,
                     menu=reply.menu, parse_mode="HTML")


def edit_reply(api, session_factory: sessionmaker[Session], clinic_id: uuid.UUID,
               chat_id: int, message_id: int, reply: Reply) -> None:
    """Редактирование сообщения-источника callback'а (П-4): календарь
    листается на месте, без спама в чат. Нумерация — та же, что в send."""
    rows = _number_buttons(session_factory, clinic_id, chat_id,
                           reply.button_rows or tuple((b,) for b in reply.buttons))
    api.edit_message_text(chat_id, message_id, reply.text, button_rows=rows,
                          parse_mode="HTML")
