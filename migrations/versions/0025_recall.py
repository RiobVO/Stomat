"""Recall: интервал повторного визита у услуги + журнал приглашений.

Пациент, которому не написали, не возвращается сам: чистка раз в полгода —
деньги клиники, о которых никто не спрашивает. `service.recall_months` — через
сколько месяцев после визита звать снова; NULL (умолчание) = по этой услуге
рассылка выключена, включает её владелец в консоли.

`recall_outreach` — журнал отправок. UNIQUE(appointment_id) держит обещание
«одна отправка на приём» в БД, а не в памяти цикла: цикл напоминаний идёт
каждые полминуты и переживает рестарт, поэтому единственная защита от второго
приглашения — уникальный ключ. Адресат хранится строкой (chat_id + язык
приёма) — новых PII-колонок нет. `booked_at` — конверсия: пациент вернулся
именно по этому приглашению, витрина /stats считает по нему деньги.
FK на appointment намеренно нет: журнал живёт своим сроком хранения, отдельным
от судьбы исходной записи. Изоляция клиник — RLS FORCE, как у admin_relay
(0022).

Revision ID: 0025
"""
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE service ADD COLUMN recall_months int")
    op.execute("""
        CREATE TABLE recall_outreach (
            id bigserial PRIMARY KEY,
            clinic_id uuid NOT NULL REFERENCES clinic(id),
            appointment_id uuid NOT NULL UNIQUE,
            tg_chat_id bigint NOT NULL,
            lang char(2) DEFAULT 'ru',
            sent_at timestamptz NOT NULL DEFAULT now(),
            booked_at timestamptz
        )
    """)
    op.execute("ALTER TABLE recall_outreach ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE recall_outreach FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON recall_outreach
        USING (clinic_id = current_setting('app.clinic_id')::uuid)
        WITH CHECK (clinic_id = current_setting('app.clinic_id')::uuid)
    """)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON recall_outreach "
               "TO navbat_app")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE recall_outreach_id_seq "
               "TO navbat_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS recall_outreach")
    op.execute("ALTER TABLE service DROP COLUMN IF EXISTS recall_months")
