#!/bin/sh
# Роль приложения: непривилегированная, НЕ владелец таблиц, НЕ bypassrls —
# RLS работает только против такой роли. Пароль из NAVBAT_APP_PASSWORD.
set -e

if [ -z "$NAVBAT_APP_PASSWORD" ]; then
    echo "[initdb] FAIL: NAVBAT_APP_PASSWORD не задан" >&2
    exit 1
fi

psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<SQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'navbat_app') THEN
        CREATE ROLE navbat_app LOGIN PASSWORD '${NAVBAT_APP_PASSWORD}';
    ELSE
        ALTER ROLE navbat_app LOGIN PASSWORD '${NAVBAT_APP_PASSWORD}';
    END IF;
END
\$\$;
SQL
