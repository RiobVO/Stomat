"""Канал Telegram одной клиники: транспорт + воркеры.

    python -m navbat.telegram --clinic <uuid>                  # long polling
    python -m navbat.telegram --clinic <uuid> --webhook-url https://host  # webhook
    ... --real   # ДЕНЬГИ: реальный gpt-4o-mini вместо фикстур спайка

Токен бота и админ-чат — в clinic (tg_bot_token_encrypted, tg_admin_chat_id);
NLU по умолчанию фейковый (фикстуры спайка) — живой smoke без расходов.
Остановка Ctrl+C: processing-апдейты дорабатываются, очередь не теряется.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from navbat.crypto import decrypt_text
from navbat.db.base import make_app_engine, make_session_factory, tenant_transaction
from navbat.dialog.fsm import DialogEngine
from navbat.nlu.extractor import FakeExtractor
from navbat.telegram.api import TelegramAPI
from navbat.telegram.escalation import TelegramEscalation
from navbat.telegram.transport import PollingTransport, WebhookServer
from navbat.telegram.worker import UpdateWorker

log = logging.getLogger("navbat.telegram")

FIXTURES = Path(__file__).parents[3] / "spike_nlu" / "data" / "messages.jsonl"


@dataclass(frozen=True)
class ClinicCredentials:
    token: str
    admin_chat_id: int | None
    webhook_secret: str | None


def load_clinic_credentials(session_factory: sessionmaker[Session],
                            clinic_id: uuid.UUID) -> ClinicCredentials:
    with tenant_transaction(session_factory, clinic_id) as session:
        row = session.execute(
            text("SELECT tg_bot_token_encrypted, tg_admin_chat_id, tg_webhook_secret "
                 "FROM clinic WHERE id = :id"),
            {"id": clinic_id},
        ).one_or_none()
    if row is None:
        sys.exit(f"[FAIL] клиника {clinic_id} не найдена")
    if not row.tg_bot_token_encrypted:
        sys.exit(f"[FAIL] у клиники {clinic_id} не задан tg_bot_token_encrypted")
    return ClinicCredentials(
        token=decrypt_text(row.tg_bot_token_encrypted),
        admin_chat_id=row.tg_admin_chat_id,
        webhook_secret=row.tg_webhook_secret,
    )


def build_dialog_extractor(use_real: bool, session_factory, clinic_id, notifier):
    """NLU для канала. --real собирает ТУ ЖЕ цепочку, что супервизор
    (деидентификация + дневной бюджет + дрейф + fallback) — голый
    OpenAIExtractor слал бы PII в LLM без маскировки (C1)."""
    if use_real:
        if not os.environ.get("OPENAI_API_KEY"):
            sys.exit("[FAIL] --real требует OPENAI_API_KEY")
        from navbat.supervisor import build_real_extractor

        log.warning("NLU: gpt-4o-mini — каждое сообщение стоит денег")
        return build_real_extractor(session_factory, clinic_id, notifier)
    if not FIXTURES.exists():
        sys.exit(f"[FAIL] нет фикстур {FIXTURES} — без --real нужен spike_nlu")
    extractor = FakeExtractor.from_fixtures(FIXTURES)
    log.info("NLU: фейковый экстрактор, %d фраз спайка (без API-вызовов)",
             len(extractor))
    return extractor


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram-канал клиники Navbat")
    parser.add_argument("--clinic", required=True, type=uuid.UUID)
    parser.add_argument("--webhook-url", default=None,
                        help="публичный https-URL; без него — long polling")
    parser.add_argument("--webhook-port", type=int, default=8443)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--real", action="store_true",
                        help="реальный gpt-4o-mini вместо фикстур (платно!)")
    args = parser.parse_args()

    if not os.environ.get("NAVBAT_ENC_KEY"):
        sys.exit("[FAIL] NAVBAT_ENC_KEY не задан — токен бота не расшифровать")

    session_factory = make_session_factory(make_app_engine())
    credentials = load_clinic_credentials(session_factory, args.clinic)
    api = TelegramAPI(credentials.token)
    me = api.get_me()
    log.info("бот @%s, клиника %s", me.get("username"), args.clinic)

    notifier = TelegramEscalation(api, credentials.admin_chat_id)
    dialog = DialogEngine(
        session_factory, args.clinic,
        extractor=build_dialog_extractor(args.real, session_factory,
                                         args.clinic, notifier),
        notifier=notifier,
    )

    stop = threading.Event()
    workers = [
        UpdateWorker(session_factory, args.clinic, dialog=dialog, api=api,
                     notifier=TelegramEscalation(api, credentials.admin_chat_id))
        for _ in range(args.workers)
    ]
    threads = [threading.Thread(target=w.run, args=(stop,), name=f"worker-{i}")
               for i, w in enumerate(workers)]
    for thread in threads:
        thread.start()

    webhook_server = None
    try:
        if args.webhook_url:
            if not credentials.webhook_secret:
                sys.exit("[FAIL] webhook-режим требует clinic.tg_webhook_secret")
            webhook_server = WebhookServer(
                session_factory, args.clinic, secret=credentials.webhook_secret,
                port=args.webhook_port,
            )
            webhook_server.start()
            api.set_webhook(args.webhook_url.rstrip("/") + webhook_server.path,
                            secret_token=credentials.webhook_secret)
            log.info("webhook установлен: %s", args.webhook_url)
            stop.wait()  # до Ctrl+C
        else:
            api.delete_webhook()  # иначе getUpdates вернёт 409
            log.info("long polling запущен, воркеров: %d", args.workers)
            PollingTransport(session_factory, args.clinic, api).run(stop)
    except KeyboardInterrupt:
        log.info("останавливаюсь…")
    finally:
        stop.set()
        if webhook_server:
            webhook_server.stop()
        for thread in threads:
            thread.join(timeout=10)
    return 0
