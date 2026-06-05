-- Роль приложения: непривилегированная, НЕ владелец таблиц, НЕ bypassrls.
-- RLS работает только против такой роли — superuser обходит политики молча.
-- Пароль dev-only; в проде роль создаётся с секретом из vault/env.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'navbat_app') THEN
        CREATE ROLE navbat_app LOGIN PASSWORD 'navbat_app_dev';
    END IF;
END
$$;
