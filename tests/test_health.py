"""Health-эндпоинт (C-2): db, очередь, календарь, cert, LLM-ключи.

Сервер тестируется живым HTTP на эфемерном порту (паттерн webhook-тестов).
Живого LLM-пинга нет by design (деньги).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import text

from navbat.health import (
    CERT_WARN_DAYS,
    HealthChecker,
    HealthServer,
    days_until_cert_expiry,
)


def _selfsigned(tmp_path, days: int) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=days))
            .sign(key, hashes.SHA256()))
    path = tmp_path / "fullchain.pem"
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(path)


def _get(server: HealthServer, query: str = "") -> httpx.Response:
    return httpx.get(f"http://127.0.0.1:{server.port}/health{query}")


def _serving(checker: HealthChecker) -> HealthServer:
    server = HealthServer(checker, host="127.0.0.1", port=0)
    server.start()
    return server


def test_healthy_clinic_returns_ok(app_session_factory, clinic_a):
    server = _serving(HealthChecker(app_session_factory, clinic_a))
    try:
        response = _get(server)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["checks"]["db"] == "ok"
    finally:
        server.stop()


def test_light_mode_checks_db_only(app_session_factory, clinic_a):
    server = _serving(HealthChecker(app_session_factory, clinic_a))
    try:
        body = _get(server, "?check=db").json()
        assert body["checks"]["db"] == "ok"
        assert "queue_oldest_pending_sec" not in body["checks"]
    finally:
        server.stop()


def test_stalled_queue_degrades(app_session_factory, admin_engine, clinic_a):
    with admin_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO message_queue (clinic_id, update_id, tg_chat_id, "
            "payload, status, created_at) VALUES (:c, 1, 100, '{}', 'pending', "
            "now() - interval '10 minutes')"), {"c": clinic_a})
    server = _serving(HealthChecker(app_session_factory, clinic_a))
    try:
        response = _get(server)
        assert response.status_code == 503
        assert response.json()["status"] == "degraded"
    finally:
        server.stop()


def test_unknown_path_is_404(app_session_factory, clinic_a):
    server = _serving(HealthChecker(app_session_factory, clinic_a))
    try:
        assert httpx.get(
            f"http://127.0.0.1:{server.port}/nope").status_code == 404
    finally:
        server.stop()


def test_stale_calendar_degrades(app_session_factory, admin_engine, clinic_a):
    with admin_engine.begin() as conn:
        conn.execute(text(
            "UPDATE clinic SET gcal_refresh_token_encrypted = 'x', "
            "gcal_last_sync_at = now() - interval '1 hour' WHERE id = :c"),
            {"c": clinic_a})
    checker = HealthChecker(app_session_factory, clinic_a, sync_interval_sec=60)
    ok, checks = checker.snapshot()
    assert ok is False
    assert "calendar" in checks


def test_calendar_not_configured_is_ok(app_session_factory, clinic_a):
    ok, checks = HealthChecker(app_session_factory, clinic_a).snapshot()
    assert ok is True
    assert checks["calendar"] == "not-configured"


def test_cert_expiry_days(tmp_path):
    assert days_until_cert_expiry(_selfsigned(tmp_path, 90)) in (88, 89, 90)
    assert days_until_cert_expiry(str(tmp_path / "missing.pem")) is None


def test_expiring_cert_degrades(app_session_factory, clinic_a, tmp_path):
    cert = _selfsigned(tmp_path, CERT_WARN_DAYS - 5)
    checker = HealthChecker(app_session_factory, clinic_a, cert_path=cert)
    ok, checks = checker.snapshot()
    assert ok is False
    assert checks["cert_days_left"] <= CERT_WARN_DAYS - 5


def test_expiring_cert_alerts_owner_once_per_day(app_session_factory, clinic_a,
                                                 tmp_path):
    from test_dialog_booking import RecordingNotifier

    cert = _selfsigned(tmp_path, CERT_WARN_DAYS - 5)
    notifier = RecordingNotifier()
    checker = HealthChecker(app_session_factory, clinic_a, cert_path=cert,
                            notifier=notifier)
    checker.snapshot()
    checker.snapshot()  # тот же день — без повтора
    assert len(notifier.calls) == 1
    assert "cert" in notifier.calls[0][1].lower()


def test_p95_reported_in_health(app_session_factory, admin_engine, clinic_a):
    with admin_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO message_queue (clinic_id, update_id, tg_chat_id, "
            "payload, status, created_at, completed_at) VALUES "
            "(:c, 1, 100, '{}', 'done', now() - interval '2 seconds', now())"),
            {"c": clinic_a})
    ok, checks = HealthChecker(app_session_factory, clinic_a).snapshot()
    assert ok is True
    assert checks["p95_response_sec_1h"] is not None
