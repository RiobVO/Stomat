#!/bin/sh
# Миграции при каждом старте (идемпотентно), затем exec: PID 1 = python,
# SIGTERM от docker stop доходит до supervisor.install_sigterm_handler.
set -e

if [ -z "$NAVBAT_ADMIN_DSN" ]; then
    echo "[entrypoint] FAIL: NAVBAT_ADMIN_DSN не задан (нужен для alembic)" >&2
    exit 1
fi

echo "[entrypoint] alembic upgrade head"
alembic upgrade head

exec python -m navbat \
    ${NAVBAT_CLINIC_ID:+--clinic "$NAVBAT_CLINIC_ID"} \
    ${NAVBAT_REAL:+--real} \
    ${NAVBAT_WORKERS:+--workers "$NAVBAT_WORKERS"} \
    ${NAVBAT_SYNC_INTERVAL:+--sync-interval "$NAVBAT_SYNC_INTERVAL"} \
    ${NAVBAT_REMINDER_OFFSETS:+--reminder-offsets "$NAVBAT_REMINDER_OFFSETS"} \
    ${NAVBAT_WEBHOOK_URL:+--webhook-url "$NAVBAT_WEBHOOK_URL"} \
    ${NAVBAT_WEBHOOK_PORT:+--webhook-port "$NAVBAT_WEBHOOK_PORT"}
