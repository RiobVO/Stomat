"""Супервизор: вся система одним процессом — канал, календарь, напоминания.

    python -m navbat                          # демо-клиника, фикстурный NLU
    python -m navbat --clinic <uuid> --real   # ДЕНЬГИ: живой gpt-4o-mini
    python -m navbat --check                  # преддемо-чеклист [OK]/[FAIL]

Потоки: polling-транспорт, N воркеров очереди, календарный sync (если
настроен Google), напоминания + вечерняя сводка. Ctrl+C — graceful.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import uuid
from datetime import timedelta
from pathlib import Path

# импорт на уровне модуля: alembic при первом импорте логирует регистрацию
# плагинов в INFO — до logging.basicConfig это уходит в никуда, а ленивый
# импорт внутри --check сорил бы в чистый вывод чеклиста
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import text

from navbat.db.base import make_app_engine, make_session_factory, tenant_transaction
from navbat.dialog.escalation import system_alert
from navbat.dialog.fsm import DialogEngine
from navbat.envfile import load_env_file
from navbat.health import HealthChecker, HealthServer
from navbat.nlu.wrappers import (
    BudgetedExtractor,
    DeidentifyingExtractor,
    DriftTrackingExtractor,
    UsageRecorder,
)
from navbat.onboard import DEMO_CLINIC_ID, DEV_ENC_KEY
from navbat.reminders import ReminderService
from navbat.telegram.api import TelegramAPI, TelegramAPIError
from navbat.telegram.app import build_dialog_extractor, load_clinic_credentials
from navbat.telegram.escalation import build_escalation
from navbat.telegram.transport import PollingTransport, WebhookServer, ensure_webhook
from navbat.telegram.worker import UpdateWorker

log = logging.getLogger("navbat")

# витрина /stats на показе: недельный посев demo_history даёт ~40 записей,
# живая репетиция — единицы; между ними и лежит порог «история стёрта»
SHOWCASE_MIN_BOOKED = 20


def migrations_head() -> str | None:
    """Head ревизия по файлам миграций; None — файлов нет (урезанная
    инсталляция) и сверка невозможна."""
    root = Path(__file__).resolve().parents[2]
    ini = root / "alembic.ini"
    if not ini.exists() or not (root / "migrations").exists():
        return None
    # пустой Config: чтение alembic.ini тянет его [loggers] и сорит
    # INFO-строками в чистый вывод --check
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(root / "migrations"))
    return ScriptDirectory.from_config(cfg).get_current_head()


def check_migrations(session_factory, clinic_id: uuid.UUID) -> tuple[bool, str]:
    """Ревизия БД == head файлов? Живая находка 12.06: проверка «таблица
    0006 существует» пропускала базу на 0018 при коде с 0019 — бот падал.

    Отдельная транзакция: permission denied (база без 0020) не должен
    отравить основную проверку --check."""
    try:
        with tenant_transaction(session_factory, clinic_id) as session:
            db_rev = session.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
    except Exception:
        # нет GRANT (0020) — база заведомо отстаёт
        return False, "alembic_version недоступна — выполните: alembic upgrade head"
    head = migrations_head()
    if head is None:
        return True, f"БД {db_rev}; файлов миграций нет — сверка пропущена"
    if db_rev == head:
        return True, f"ревизия {db_rev} = head"
    return False, (f"БД {db_rev}, код ждёт {head} — "
                   f"выполните: alembic upgrade head")


def parse_offsets(raw: str) -> tuple[timedelta, ...]:
    """«1440,120» (минуты) → офсеты напоминаний. Демо: «2,1»."""
    offsets = tuple(timedelta(minutes=int(part.strip()))
                    for part in raw.split(",") if part.strip())
    if not offsets:
        raise ValueError(f"пустой список офсетов: {raw!r}")
    return offsets


def install_sigterm_handler(stop: threading.Event) -> None:
    """docker stop шлёт SIGTERM — гасим теми же рельсами, что Ctrl+C."""
    signal.signal(signal.SIGTERM, lambda signum, frame: stop.set())


def supervise_threads(threads, stop: threading.Event, notifier=None,
                      interval: float = 5.0, died: list[str] | None = None
                      ) -> list[str]:
    """Надзор за фоновыми потоками; возвращает имена умерших.

    Вся работа системы, кроме транспорта, живёт потоками одного процесса.
    Умерший поток процесс не гасит: главный поток продолжает крутить polling
    или ждать SIGTERM, docker-healthcheck ходит в light-ветку /health и
    остаётся зелёным, а очередь никто не разбирает — бот молчит, и об этом
    не узнаёт никто. Поэтому смерть потока = сигнал владельцу системы и
    остановка процесса: контейнер поднимется заново (restart-политика
    compose), а не будет изображать живого.
    """
    while not stop.wait(interval):
        dead = [thread.name for thread in threads if not thread.is_alive()]
        if dead:
            names = ", ".join(dead)
            # список заполняется ДО stop: главный поток проснётся от stop и
            # сразу решает, чем кончился запуск — авария это или Ctrl+C
            if died is not None:
                died.extend(dead)
            log.error("фоновый поток умер: %s — останавливаю процесс", names)
            try:
                if notifier is not None:
                    system_alert(
                        notifier,
                        f"фоновый поток умер: {names} — процесс остановлен, "
                        f"контейнер должен подняться заново", {})
            except Exception:
                # недоставленный алерт не отменяет остановку: молча работающий
                # процесс хуже, чем процесс без уведомления
                log.exception("надзор: алерт о смерти потока не ушёл")
            finally:
                stop.set()
            return dead
    return []


def validate_real_env() -> list[str]:
    """--real = боевой режим: PII под dev-ключом и пустые API-ключи — отказ."""
    problems = []
    enc_key = os.environ.get("NAVBAT_ENC_KEY")
    if not enc_key or enc_key == DEV_ENC_KEY:
        problems.append(
            "NAVBAT_ENC_KEY: для --real нужен боевой ключ (base64 от 32 байт),"
            " dev-ключ недопустим")
    if not os.environ.get("OPENAI_API_KEY"):
        problems.append("OPENAI_API_KEY не задан — --real без него не работает")
    return problems


def build_real_extractor(session_factory, clinic_id: uuid.UUID, notifier):
    """Боевая сборка NLU: бюджет → деидентификация → fallback(OpenAI, Gemini).

    Деидентификация и бюджет общие на оба провайдера; без GEMINI_API_KEY
    каскада нет — аутэйдж OpenAI уходит в ретрай очереди (как раньше).
    """
    from navbat.nlu.openai_extractor import OpenAIExtractor

    recorder = UsageRecorder(session_factory, clinic_id, notifier=notifier)
    prompt = _load_pinned_prompt(session_factory, clinic_id)
    extractor = OpenAIExtractor(on_usage=recorder.record,
                                on_repair=recorder.record_repair,
                                prompt=prompt)
    if os.environ.get("GEMINI_API_KEY"):
        from navbat.nlu.fallback import FallbackExtractor
        from navbat.nlu.gemini_extractor import GeminiExtractor

        extractor = FallbackExtractor(
            extractor, GeminiExtractor(on_usage=recorder.record,
                                       on_repair=recorder.record_repair,
                                       prompt=prompt))
        log.info("LLM-fallback включён: Gemini")
    else:
        log.warning("GEMINI_API_KEY не задан — fallback-LLM выключен")
    inner = DriftTrackingExtractor(DeidentifyingExtractor(extractor), recorder)
    return BudgetedExtractor(inner, recorder)


def _load_pinned_prompt(session_factory, clinic_id: uuid.UUID) -> str | None:
    """Версия NLU-промпта из БД по пину клиники; None — встроенный файл (B.2)."""
    with tenant_transaction(session_factory, clinic_id) as session:
        row = session.execute(text(
            "SELECT p.version, p.body FROM clinic c "
            "JOIN nlu_prompt p ON p.version = c.nlu_prompt_version "
            "WHERE c.id = current_setting('app.clinic_id')::uuid"
        )).one_or_none()
    if row is None:
        log.info("NLU-промпт: встроенный файл")
        return None
    log.info("NLU-промпт: версия %d из БД", row.version)
    return row.body


def run_check(session_factory, clinic_id: uuid.UUID, use_real: bool) -> int:
    """Преддемо-чеклист. Возвращает exit code."""
    failures = 0

    def report(ok: bool, label: str, detail: str = "") -> None:
        nonlocal failures
        if not ok:
            failures += 1
        suffix = f" — {detail}" if detail else ""
        print(f"{'[OK]' if ok else '[FAIL]'} {label}{suffix}")

    try:
        with tenant_transaction(session_factory, clinic_id) as session:
            session.execute(text("SELECT 1 FROM reminder LIMIT 0"))  # миграции 0006+
            clinic = session.execute(
                text("SELECT name, tg_bot_token_encrypted, tg_admin_chat_ids, "
                     "gcal_refresh_token_encrypted, nlu_prompt_version, "
                     "address, payment_info, phone, bot_paused, llm_enabled "
                     "FROM clinic WHERE id = :id"),
                {"id": clinic_id},
            ).one_or_none()
            doctors = session.execute(
                text("SELECT count(*) FROM doctor WHERE is_active")).scalar_one()
            services = session.execute(
                text("SELECT count(*) FROM service WHERE is_active")).scalar_one()
            # привязка календаря живёт на враче: живой токен клиники сам по себе
            # не значит, что записи уедут в Google (карта продажи, №1).
            # Два счётчика, а не один: синк и watch выбирают врачей БЕЗ
            # is_active (sync_loop.py:48, watch.py:41 — контракт инкремента 2,
            # существующие записи скрытого врача продолжают синхаться), а до
            # пациента доходят только активные — состояния «не привязан вовсе»
            # и «привязан только скрытому» требуют разных подсказок
            bound_total, bound_active = session.execute(text(
                "SELECT count(*) FILTER (WHERE gcal_calendar_id IS NOT NULL), "
                "count(*) FILTER (WHERE gcal_calendar_id IS NOT NULL AND is_active) "
                "FROM doctor")).one()
        report(True, "БД и миграции")
    except Exception as e:
        report(False, "БД и миграции", str(e)[:120])
        return 1

    mig_ok, mig_detail = check_migrations(session_factory, clinic_id)
    report(mig_ok, "ревизия миграций = head", mig_detail)

    if clinic is None:
        report(False, "клиника", f"{clinic_id} не найдена — python -m navbat.onboard")
        return 1
    report(True, "клиника", clinic.name)
    report(doctors > 0 and services > 0, "врачи и услуги",
           f"{doctors} врач(а), {services} услуг")
    # подсказки, не FAIL: без поля бот на такой вопрос отвечает меню
    # (П-2б, полировка-2)
    report(True, "адрес клиники (FAQ «где вы находитесь?»)",
           clinic.address or "не задан — onboard --address")
    report(True, "оплата (FAQ «можно картой?»)",
           clinic.payment_info or "не задана — onboard --payment")
    report(True, "телефон клиники (FAQ «какой номер?»)",
           clinic.phone or "не задан — onboard --phone")

    if clinic.tg_bot_token_encrypted:
        from navbat.crypto import decrypt_text
        try:
            me = TelegramAPI(decrypt_text(clinic.tg_bot_token_encrypted)).get_me()
            report(True, "Telegram-бот", f"@{me.get('username')}")
        except TelegramAPIError as e:
            report(False, "Telegram-бот", str(e)[:120])
    else:
        report(False, "Telegram-бот", "токен не задан (onboard --tg-token)")
    admin_chats = clinic.tg_admin_chat_ids or []
    report(bool(admin_chats),
           "админ-чат (эскалации, /stats, сводка)",
           f"{len(admin_chats)} чат(ов)" if admin_chats else None)

    # рубильники (C-4): бот на паузе отвечает «запись приостановлена» на любое
    # сообщение, /llm off уводит свободный текст в меню — оба состояния
    # переживают рестарт и до этой строки чеклист их не видел
    report(not clinic.bot_paused, "рубильник: пауза бота",
           "бот на паузе — вернуть: /resume" if clinic.bot_paused
           else "бот принимает запись")
    report(clinic.llm_enabled, "рубильник: LLM-рубильник",
           "LLM выключен — свободный текст уходит в меню, вернуть: /llm on"
           if not clinic.llm_enabled else "свободный текст понимает NLU")

    if not clinic.gcal_refresh_token_encrypted:
        # клиника без календаря — законный режим, но цену надо назвать вслух
        report(True, "Google Calendar",
               "не настроен — бот работает без календаря, событий в Google "
               "не будет (python -m navbat.calendar.auth)")
    elif not bound_total:
        report(False, "Google Calendar",
               "токен есть, но календарь не привязан ни одному врачу — "
               "записи никуда не уедут: onboard --doctor <uuid> --calendar <id>")
    elif not bound_active:
        # токен не проверяем: записи к скрытому врачу не идут в любом случае,
        # а его календарь синк всё равно ведёт и об OAuth-сбое алертит сам
        report(False, "Google Calendar",
               f"календарь привязан только скрытым врачам ({bound_total}) — "
               f"пациентам они не предлагаются, событий не будет: "
               f"верните врача кнопкой «✅ Показать» в админ-чате")
    else:
        from navbat.calendar.api import CalendarAuthError, GoogleCalendarAPI
        from navbat.crypto import decrypt_text
        try:
            GoogleCalendarAPI(decrypt_text(clinic.gcal_refresh_token_encrypted)).check_auth()
            report(True, "Google Calendar (refresh-токен жив)",
                   f"врачей с календарём: {bound_active}")
        except CalendarAuthError as e:
            report(False, "Google Calendar", str(e)[:120])

    if os.environ.get("GEMINI_API_KEY"):
        report(True, "fallback-LLM", "Gemini (ключ задан)")
    else:
        report(True, "fallback-LLM",
               "не настроен — при аутэйдже OpenAI бот без NLU (GEMINI_API_KEY)")
    report(True, "NLU-промпт",
           f"версия {clinic.nlu_prompt_version} (БД)"
           if clinic.nlu_prompt_version else "встроенный файл")

    if clinic_id == DEMO_CLINIC_ID:
        # pytest стирает демо-историю, а финализатор сьюта возвращает клинику
        # БЕЗ неё — 31.07 сводка показа вышла с «записей: 2», заметили только
        # глазами. Судим тем же collect_stats, что и витрина (конвенция);
        # порог ниже недельного посева demo_history (~40), но выше следов
        # живой репетиции
        from navbat.demo_history import summary_stats
        window = summary_stats(session_factory, clinic_id, days=7)
        booked = window.booked if window else 0
        report(booked >= SHOWCASE_MIN_BOOKED, "витрина /stats (демо-история)",
               f"записей за 7 дн: {booked}" if booked >= SHOWCASE_MIN_BOOKED
               else f"записей за 7 дн: {booked} — /stats на показе будет "
                    f"пустым: python -m navbat.onboard --demo-history")

    if use_real:
        report(bool(os.environ.get("OPENAI_API_KEY")), "OPENAI_API_KEY для --real")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Navbat: канал + календарь + напоминания одним процессом")
    parser.add_argument("--clinic", type=uuid.UUID, default=DEMO_CLINIC_ID,
                        help="по умолчанию — демо-клиника")
    parser.add_argument("--real", action="store_true",
                        help="реальный gpt-4o-mini (платно!)")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--reminder-offsets", default="1440,120",
                        help="минуты до приёма, CSV; демо: 2,1")
    parser.add_argument("--sync-interval", type=int, default=60)
    parser.add_argument("--no-calendar", action="store_true")
    parser.add_argument("--webhook-url", default=None,
                        help="публичный https-URL; без него — long polling")
    parser.add_argument("--webhook-port", type=int, default=8443)
    parser.add_argument("--health-port", type=int,
                        default=int(os.environ.get("NAVBAT_HEALTH_PORT", "8080")))
    parser.add_argument("--check", action="store_true",
                        help="преддемо-чеклист и выход")
    args = parser.parse_args()

    load_env_file()
    if args.real and not args.check:
        problems = validate_real_env()
        if problems:
            for problem in problems:
                print(f"[FAIL] {problem}")
            return 1
    os.environ.setdefault("NAVBAT_ENC_KEY", DEV_ENC_KEY)
    session_factory = make_session_factory(make_app_engine())

    if args.check:
        return run_check(session_factory, args.clinic, args.real)

    offsets = parse_offsets(args.reminder_offsets)
    credentials = load_clinic_credentials(session_factory, args.clinic)
    tg_api = TelegramAPI(credentials.token)
    me = tg_api.get_me()
    notifier = build_escalation(tg_api, session_factory, args.clinic,
                                credentials.admin_chat_ids)
    log.info("бот @%s, клиника %s", me.get("username"), args.clinic)

    extractor = build_dialog_extractor(args.real, session_factory,
                                       args.clinic, notifier)

    # календарь: sync-цикл + freeBusy-guard перед confirm
    slot_guard = None
    calendar_sync = None
    watch_manager = None
    with tenant_transaction(session_factory, args.clinic) as session:
        gcal_token = session.execute(
            text("SELECT gcal_refresh_token_encrypted FROM clinic WHERE id = :id"),
            {"id": args.clinic},
        ).scalar_one_or_none()
    if gcal_token and not args.no_calendar:
        from navbat.calendar.guard import CalendarSlotGuard
        from navbat.calendar.sync import CalendarSync
        from navbat.calendar.api import GoogleCalendarAPI
        from navbat.crypto import decrypt_text

        gcal_api = GoogleCalendarAPI(decrypt_text(gcal_token))
        slot_guard = CalendarSlotGuard(session_factory, args.clinic, gcal_api)
        calendar_sync = CalendarSync(session_factory, args.clinic, api=gcal_api,
                                     notifier=notifier, tg_api=tg_api)
        log.info("календарь: sync каждые %d с + freeBusy-guard", args.sync_interval)
        if args.webhook_url:
            from navbat.calendar.watch import GcalWatchManager

            watch_manager = GcalWatchManager(session_factory, args.clinic,
                                             gcal_api, args.webhook_url)
            log.info("календарь: watch-каналы включены (push будит синк)")
    else:
        log.info("календарь: выключен")

    dialog = DialogEngine(session_factory, args.clinic, extractor=extractor,
                          notifier=notifier, slot_guard=slot_guard)
    reminders = ReminderService(session_factory, args.clinic, tg_api=tg_api,
                                notifier=notifier, offsets=offsets,
                                digest_chat_id=credentials.admin_chat_ids)

    stop = threading.Event()
    sync_wake = threading.Event()  # push /gcal/push/<канал> будит календарь
    install_sigterm_handler(stop)
    threads = [
        threading.Thread(target=reminders.run, args=(stop,), name="reminders"),
    ]
    for index in range(args.workers):
        worker = UpdateWorker(session_factory, args.clinic, dialog=dialog,
                              api=tg_api, notifier=notifier,
                              admin_chat_id=credentials.admin_chat_ids)
        threads.append(threading.Thread(target=worker.run, args=(stop,),
                                        name=f"worker-{index}"))
    if calendar_sync is not None:
        from navbat.calendar.sync_loop import CalendarSyncLoop

        sync_loop = CalendarSyncLoop(session_factory, args.clinic, calendar_sync,
                                     notifier, credentials.admin_chat_ids)

        def calendar_loop() -> None:
            while not stop.is_set():
                if watch_manager is not None:
                    try:
                        watch_manager.ensure_channels()
                    except Exception:
                        log.exception("watch-каналы: ensure_channels упал")
                try:
                    # run_once открывает транзакцию за списком врачей вне
                    # собственных except: обрыв соединения с БД уносил бы
                    # календарный поток целиком и навсегда
                    sync_loop.run_once()
                except Exception:
                    log.exception("календарный цикл упал — продолжаю")
                sync_wake.wait(args.sync_interval)
                sync_wake.clear()

        threads.append(threading.Thread(target=calendar_loop, name="calendar"))

    for thread in threads:
        thread.start()
    log.info("система поднята: %d воркера, напоминания %s",
             args.workers, args.reminder_offsets)

    # надзор: умерший поток гасит процесс, иначе бот молчит, а контейнер
    # выглядит здоровым (light-ветка /health смотрит только на БД)
    died: list[str] = []
    watchdog = threading.Thread(
        target=supervise_threads,
        args=(threads, stop), kwargs={"notifier": notifier, "died": died},
        name="watchdog", daemon=True)
    watchdog.start()

    health = HealthServer(
        HealthChecker(session_factory, args.clinic,
                      sync_interval_sec=args.sync_interval,
                      cert_path=os.environ.get("NAVBAT_CERT_PATH"),
                      notifier=notifier,
                      backup_dir=os.environ.get("NAVBAT_BACKUP_DIR"),
                      backup_interval_sec=int(os.environ.get(
                          "NAVBAT_BACKUP_INTERVAL_SEC", "7200"))),
        port=args.health_port)
    health.start()

    webhook_server = None
    try:
        if args.webhook_url:
            if not credentials.webhook_secret:
                sys.exit("[FAIL] webhook-режим требует webhook-секрет "
                         "(onboard --tg-token генерирует)")
            webhook_server = WebhookServer(
                session_factory, args.clinic,
                secret=credentials.webhook_secret, port=args.webhook_port,
                gcal_wake=sync_wake if calendar_sync is not None else None)
            webhook_server.start()
            ensure_webhook(tg_api, args.webhook_url,
                           credentials.webhook_secret,
                           notifier=notifier, path=webhook_server.path)
            stop.wait()  # до SIGTERM/Ctrl+C
        else:
            tg_api.delete_webhook()  # иначе getUpdates вернёт 409
            PollingTransport(session_factory, args.clinic, tg_api).run(stop)
    except KeyboardInterrupt:
        log.info("останавливаюсь…")
    finally:
        stop.set()
        sync_wake.set()  # разбудить календарный поток, чтобы он увидел stop
        if webhook_server:
            webhook_server.stop()
        health.stop()
        for thread in threads:
            thread.join(timeout=10)
    # ненулевой код: остановка из-за умершего потока — это авария, а не
    # штатное завершение, и в логе контейнера она должна читаться как авария
    return 1 if died else 0
