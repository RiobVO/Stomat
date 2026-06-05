"""Инкремент 5: напоминания, учёт LLM-токенов, отметка вечерней сводки.

reminder: вычисляются reconciliation'ом из appointment (не таймеры в памяти,
BRIEF) — переживают рестарт; UNIQUE (appointment_id, kind) делает upsert
идемпотентным. llm_usage: дневной учёт токенов на клинику (cap + сводка).

Revision ID: 0006
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE reminder (
            id bigserial PRIMARY KEY,
            clinic_id uuid NOT NULL REFERENCES clinic(id),
            appointment_id uuid NOT NULL REFERENCES appointment(id),
            kind text NOT NULL,
            send_at timestamptz NOT NULL,
            status text NOT NULL DEFAULT 'pending',
            attempts int NOT NULL DEFAULT 0,
            sent_at timestamptz,
            UNIQUE (appointment_id, kind),
            CONSTRAINT reminder_status_check
                CHECK (status IN ('pending', 'sent', 'cancelled', 'failed'))
        )
    """)
    op.execute("""
        CREATE INDEX ix_reminder_due ON reminder (clinic_id, send_at)
        WHERE status = 'pending'
    """)
    op.execute("""
        CREATE TABLE llm_usage (
            clinic_id uuid NOT NULL REFERENCES clinic(id),
            day date NOT NULL,
            requests int NOT NULL DEFAULT 0,
            in_tokens bigint NOT NULL DEFAULT 0,
            out_tokens bigint NOT NULL DEFAULT 0,
            PRIMARY KEY (clinic_id, day)
        )
    """)
    op.execute("ALTER TABLE clinic ADD COLUMN last_digest_date date")

    for table in ("reminder", "llm_usage"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (clinic_id = current_setting('app.clinic_id')::uuid)
            WITH CHECK (clinic_id = current_setting('app.clinic_id')::uuid)
        """)
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO navbat_app")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE reminder_id_seq TO navbat_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS llm_usage")
    op.execute("DROP TABLE IF EXISTS reminder")
    op.execute("ALTER TABLE clinic DROP COLUMN IF EXISTS last_digest_date")
