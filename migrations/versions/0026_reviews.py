"""Отзывы: журнал просьб об оценке приёма + ссылка клиники на публичный отзыв.

Недовольный пациент молча уходит писать в Google — и владелец узнаёт об этом
последним. `review` — журнал «спросили / что ответил»: UNIQUE(appointment_id)
держит обещание «одна просьба и одна оценка на приём» в БД, а не в памяти
цикла (цикл напоминаний идёт каждые полминуты и переживает рестарт).
`rating` NULL — просьба ушла, пациент ещё не нажал; CHECK 1..5 — последний
рубеж: оценка приходит из callback_data, то есть от клиента.
Адресат хранится строкой (chat_id + язык приёма) — новых PII-колонок нет.
FK на appointment намеренно нет, как у recall_outreach (0025): журнал живёт
своим сроком хранения, отдельным от судьбы исходной записи.
`clinic.review_url` — ссылка на площадку отзывов (onboard --review-url); NULL
(умолчание) = благодарность без ссылки. Изоляция клиник — RLS FORCE.

Revision ID: 0026
"""
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE clinic ADD COLUMN review_url text")
    op.execute("""
        CREATE TABLE review (
            id bigserial PRIMARY KEY,
            clinic_id uuid NOT NULL REFERENCES clinic(id),
            appointment_id uuid NOT NULL UNIQUE,
            tg_chat_id bigint NOT NULL,
            lang char(2) DEFAULT 'ru',
            rating int CHECK (rating BETWEEN 1 AND 5),
            requested_at timestamptz NOT NULL DEFAULT now(),
            rated_at timestamptz
        )
    """)
    op.execute("ALTER TABLE review ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE review FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON review
        USING (clinic_id = current_setting('app.clinic_id')::uuid)
        WITH CHECK (clinic_id = current_setting('app.clinic_id')::uuid)
    """)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON review TO navbat_app")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE review_id_seq TO navbat_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS review")
    op.execute("ALTER TABLE clinic DROP COLUMN IF EXISTS review_url")
