"""Health-эндпоинт: docker healthcheck + ручная диагностика (C-2).

Наружу НЕ публикуется (nginx его не проксирует) — только внутренняя
сеть compose. Живой LLM-пинг сознательно НЕ делается (правило денег):
вместо него наличие ключей + дневная доля сбоев NLU из llm_usage.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from navbat.db.base import tenant_transaction
from navbat.dialog.escalation import system_alert

log = logging.getLogger("navbat.health")

QUEUE_STALL_SEC = 120   # pending старше — очередь стоит
CERT_WARN_DAYS = 14     # cert истекает раньше — degraded
SYNC_AGE_FACTOR = 3     # возраст синка > N интервалов — синк мёртв


def days_until_cert_expiry(cert_path: str) -> int | None:
    """Дни до истечения серта; None — файла нет или он нечитаем."""
    from cryptography import x509
    try:
        cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
    except (OSError, ValueError):
        return None
    return (cert.not_valid_after_utc - datetime.now(timezone.utc)).days


class HealthChecker:
    """Снимок здоровья одной клиники. Каждая проверка — отдельный ключ
    в checks; статус degraded, если хоть одна критичная провалена."""

    def __init__(self, session_factory: sessionmaker[Session],
                 clinic_id: uuid.UUID, *, sync_interval_sec: int = 60,
                 cert_path: str | None = None, notifier=None) -> None:
        self._session_factory = session_factory
        self._clinic_id = clinic_id
        self._sync_interval_sec = sync_interval_sec
        self._cert_path = cert_path
        self._notifier = notifier
        self._cert_alerted_on: date | None = None  # алерт раз в день

    def snapshot(self, light: bool = False) -> tuple[bool, dict]:
        checks: dict = {}
        ok = self._check_db(checks)
        if light or not ok:
            return ok, checks
        ok = self._check_queue(checks) and ok
        ok = self._check_calendar(checks) and ok
        ok = self._check_cert(checks) and ok
        self._report_llm(checks)
        self._report_p95(checks)
        return ok, checks

    def _check_db(self, checks: dict) -> bool:
        try:
            with tenant_transaction(self._session_factory, self._clinic_id) as s:
                s.execute(text("SELECT 1"))
            checks["db"] = "ok"
            return True
        except Exception as e:
            checks["db"] = f"fail: {str(e)[:120]}"
            return False

    def _check_queue(self, checks: dict) -> bool:
        with tenant_transaction(self._session_factory, self._clinic_id) as s:
            oldest = s.execute(text(
                "SELECT extract(epoch FROM (now() - min(created_at))) "
                "FROM message_queue WHERE status = 'pending'")).scalar_one()
        age = int(oldest or 0)
        checks["queue_oldest_pending_sec"] = age
        return age <= QUEUE_STALL_SEC

    def _check_calendar(self, checks: dict) -> bool:
        with tenant_transaction(self._session_factory, self._clinic_id) as s:
            row = s.execute(text(
                "SELECT gcal_refresh_token_encrypted IS NOT NULL AS configured, "
                "extract(epoch FROM (now() - gcal_last_sync_at)) AS age "
                "FROM clinic "
                "WHERE id = current_setting('app.clinic_id')::uuid")).one()
        if not row.configured:
            checks["calendar"] = "not-configured"
            return True
        if row.age is None:
            # настроен, но ни одного успешного цикла с запуска — даём время
            checks["calendar"] = "never-synced"
            return True
        checks["calendar"] = f"synced {int(row.age)}s ago"
        return row.age <= self._sync_interval_sec * SYNC_AGE_FACTOR

    def _check_cert(self, checks: dict) -> bool:
        if not self._cert_path:
            checks["cert_days_left"] = "not-configured"
            return True
        days = days_until_cert_expiry(self._cert_path)
        if days is None:
            # серт ещё не выписан (первый старт до certbot) — не валим
            checks["cert_days_left"] = "missing"
            return True
        checks["cert_days_left"] = days
        if days < CERT_WARN_DAYS and self._notifier is not None:
            today = datetime.now(timezone.utc).date()
            if self._cert_alerted_on != today:
                self._cert_alerted_on = today
                system_alert(
                    self._notifier,
                    f"TLS-cert истекает через {days} дн. — проверьте certbot "
                    f"(renewal каждые 12 ч в compose)", {})
        return days >= CERT_WARN_DAYS

    def _report_llm(self, checks: dict) -> None:
        """Информационно: ключи и доля сбоев NLU за сегодня (не валит статус —
        деградацию NLU ловит дрифт-алерт, здесь только видимость)."""
        with tenant_transaction(self._session_factory, self._clinic_id) as s:
            row = s.execute(text(
                "SELECT requests, failures FROM llm_usage "
                "WHERE day = current_date "
                "AND clinic_id = current_setting('app.clinic_id')::uuid"
            )).one_or_none()
        checks["llm"] = {
            "openai_key": bool(os.environ.get("OPENAI_API_KEY")),
            "gemini_key": bool(os.environ.get("GEMINI_API_KEY")),
            "nlu_today": (f"{row.failures}/{row.requests} сбоев"
                          if row else "0/0 сбоев"),
        }

    def _report_p95(self, checks: dict) -> None:
        """Информационно: p95 ответа за час (SLA-видимость, статус не валит)."""
        with tenant_transaction(self._session_factory, self._clinic_id) as s:
            p95 = s.execute(text(
                "SELECT extract(epoch FROM percentile_cont(0.95) "
                "WITHIN GROUP (ORDER BY completed_at - created_at)) "
                "FROM message_queue WHERE status = 'done' "
                "AND completed_at > now() - interval '1 hour'")).scalar_one()
        checks["p95_response_sec_1h"] = (round(float(p95), 1)
                                         if p95 is not None else None)


class HealthServer:
    """Паттерн WebhookServer: stdlib-сервер, мгновенный ответ, штатный стоп."""

    def __init__(self, checker: HealthChecker, host: str = "0.0.0.0",
                 port: int = 8080) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 — API stdlib
                path, _, query = self.path.partition("?")
                if path != "/health":
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                ok, checks = outer._checker.snapshot(light="check=db" in query)
                body = json.dumps({"status": "ok" if ok else "degraded",
                                   "checks": checks}, ensure_ascii=False).encode()
                self.send_response(200 if ok else 503)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args) -> None:
                log.debug("health: " + fmt, *args)

        self._checker = checker
        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="health", daemon=True)
        self._thread.start()
        log.info("health-сервер слушает :%d/health", self.port)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)
