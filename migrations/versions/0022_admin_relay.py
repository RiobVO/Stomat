"""Якоря свайп-ответов админа: (админ-чат, сообщение) → пациент.

Админ отвечает пациенту реплаем на алерт эскалации, а Bot API отдаёт только id
сообщения, на которое ответили: без якоря ответ некуда доставить. Парсинг
chat_id из текста алерта отвергнут — шаблоны двуязычные и меняются. Якорь живёт
дни (пока идёт переписка по эскалации), старые чистит retention. RLS-изоляция
по клинике, как waitlist (0019).

Revision ID: 0022
"""
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE admin_relay (
            id bigserial PRIMARY KEY,
            clinic_id uuid NOT NULL REFERENCES clinic(id),
            admin_chat_id bigint NOT NULL,
            message_id bigint NOT NULL,
            patient_chat_id bigint NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (clinic_id, admin_chat_id, message_id)
        )
    """)
    op.execute("ALTER TABLE admin_relay ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE admin_relay FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON admin_relay
        USING (clinic_id = current_setting('app.clinic_id')::uuid)
        WITH CHECK (clinic_id = current_setting('app.clinic_id')::uuid)
    """)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON admin_relay "
               "TO navbat_app")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE admin_relay_id_seq "
               "TO navbat_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS admin_relay")
