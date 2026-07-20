"""Инкремент 1: схема scheduling engine.

Ключевые гарантии уровня БД:
- exclusion constraint: пересечение записей одного врача невозможно,
  буфер учтён в выражении constraint (конец диапазона + buffer_min);
- RLS на всех таблицах (FORCE — политика действует и на владельца);
- идемпотентность Telegram-сообщений: UNIQUE (tg_chat_id, tg_message_id).

Revision ID: 0001
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Таблицы с колонкой clinic_id — политика по ней; clinic — по собственному id
TENANT_TABLES = ["service", "doctor", "holiday", "patient", "appointment", "appointment_audit"]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute(
        "CREATE TYPE appt_status AS ENUM ('hold','booked','cancelled','done','expired')"
    )

    op.execute("""
        CREATE TABLE clinic (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name text NOT NULL,
            salt text,
            timezone text NOT NULL DEFAULT 'Asia/Tashkent'
        )
    """)
    op.execute("""
        CREATE TABLE service (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            clinic_id uuid NOT NULL REFERENCES clinic(id),
            name text NOT NULL,
            aliases text[] NOT NULL DEFAULT '{}',
            duration_min int NOT NULL,
            price numeric
        )
    """)
    op.execute("""
        CREATE TABLE doctor (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            clinic_id uuid NOT NULL REFERENCES clinic(id),
            name_encrypted text,
            working_intervals jsonb NOT NULL DEFAULT '{}',
            buffer_min int NOT NULL DEFAULT 10
        )
    """)
    op.execute("""
        CREATE TABLE holiday (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            clinic_id uuid NOT NULL REFERENCES clinic(id),
            date date NOT NULL,
            reason text
        )
    """)
    op.execute("""
        CREATE TABLE patient (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            clinic_id uuid NOT NULL REFERENCES clinic(id),
            contact_hash text,
            name_encrypted text,
            tg_chat_id bigint
        )
    """)
    # time_range хранит РЕАЛЬНОЕ время приёма; буфер — снапшот buffer_min врача
    # на момент вставки, учитывается только в выражении constraint (конец + буфер).
    # Протухший hold физически блокирует вставку (now() в predicate нельзя) —
    # движок обязан экспирить пересекающиеся протухшие hold перед INSERT.
    op.execute("""
        CREATE TABLE appointment (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            clinic_id uuid NOT NULL REFERENCES clinic(id),
            doctor_id uuid NOT NULL REFERENCES doctor(id),
            service_id uuid REFERENCES service(id),
            patient_id uuid REFERENCES patient(id),
            time_range tstzrange NOT NULL,
            buffer_min int NOT NULL DEFAULT 0,
            status appt_status NOT NULL DEFAULT 'hold',
            source text NOT NULL DEFAULT 'bot',
            lang char(2) DEFAULT 'ru',
            hold_expires_at timestamptz,
            tg_chat_id bigint,
            tg_message_id bigint,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tg_chat_id, tg_message_id),
            -- timezone('UTC', ...) — единственный IMMUTABLE-путь к арифметике
            -- с timestamptz в выражении индекса (timestamptz + interval — STABLE)
            CONSTRAINT appointment_no_overlap EXCLUDE USING gist (
                doctor_id WITH =,
                tsrange(
                    timezone('UTC', lower(time_range)),
                    timezone('UTC', upper(time_range)) + (buffer_min * interval '1 minute')
                ) WITH &&
            ) WHERE (status IN ('hold','booked'))
        )
    """)
    op.execute("""
        CREATE TABLE appointment_audit (
            id bigserial PRIMARY KEY,
            clinic_id uuid NOT NULL,
            appointment_id uuid NOT NULL,
            actor text NOT NULL,
            action text NOT NULL,
            before jsonb,
            after jsonb,
            at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_appointment_doctor_range ON appointment USING gist (doctor_id, time_range)")
    op.execute("CREATE INDEX ix_holiday_clinic_date ON holiday (clinic_id, date)")

    # RLS: current_setting БЕЗ missing_ok — запрос без тенант-контекста падает,
    # а не молча возвращает пусто. FORCE — политика действует и на владельца.
    op.execute("ALTER TABLE clinic ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE clinic FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON clinic
        USING (id = current_setting('app.clinic_id')::uuid)
        WITH CHECK (id = current_setting('app.clinic_id')::uuid)
    """)
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (clinic_id = current_setting('app.clinic_id')::uuid)
            WITH CHECK (clinic_id = current_setting('app.clinic_id')::uuid)
        """)

    op.execute("GRANT USAGE ON SCHEMA public TO navbat_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON clinic, service, doctor, holiday, patient, appointment, appointment_audit TO navbat_app")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO navbat_app")


def downgrade() -> None:
    for table in [*reversed(TENANT_TABLES), "clinic"]:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP TYPE IF EXISTS appt_status")
