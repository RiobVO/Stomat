"""Лист ожидания: нет слотов → пациент в очередь → при отмене бот предлагает
освободившийся слот.

Цель очереди — «любой ближайший слот» для услуги (по любому врачу, без
doctor_id). Частичный UNIQUE не даёт пациенту плодить дубли на одну услугу;
индекс матчера сканирует только активные записи. RLS-изоляция по клинике,
как unanswered_question (0016).

Revision ID: 0019
"""
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE waitlist (
            id bigserial PRIMARY KEY,
            clinic_id uuid NOT NULL REFERENCES clinic(id),
            service_id uuid NOT NULL REFERENCES service(id),
            tg_chat_id bigint NOT NULL,
            patient_id uuid REFERENCES patient(id),
            lang text NOT NULL DEFAULT 'ru',
            status text NOT NULL DEFAULT 'waiting',
            notified_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT waitlist_status_check CHECK (status IN
                ('waiting', 'notified', 'fulfilled', 'cancelled', 'expired'))
        )
    """)
    # дедуп: один пациент — одна активная запись на услугу
    op.execute("CREATE UNIQUE INDEX ux_waitlist_active ON waitlist "
               "(clinic_id, tg_chat_id, service_id) "
               "WHERE status IN ('waiting', 'notified')")
    # скан матчера — только активные, oldest-first
    op.execute("CREATE INDEX ix_waitlist_active ON waitlist "
               "(clinic_id, status, created_at) "
               "WHERE status IN ('waiting', 'notified')")
    op.execute("ALTER TABLE waitlist ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE waitlist FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON waitlist
        USING (clinic_id = current_setting('app.clinic_id')::uuid)
        WITH CHECK (clinic_id = current_setting('app.clinic_id')::uuid)
    """)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON waitlist TO navbat_app")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE waitlist_id_seq TO navbat_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS waitlist")
