"""Инкремент 3: Telegram-канал — durable-очередь апдейтов + реквизиты бота.

message_queue: ack только после успешной обработки (двухфазный клейм
pending→processing→done), идемпотентность по UNIQUE (clinic_id, update_id),
per-chat порядок обеспечивает клейм-запрос (см. navbat/telegram/queue.py).
Токен бота — шифртекст (navbat.crypto), по боту на клинику.

Revision ID: 0003
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE clinic ADD COLUMN tg_bot_token_encrypted text")
    op.execute("ALTER TABLE clinic ADD COLUMN tg_admin_chat_id bigint")
    op.execute("ALTER TABLE clinic ADD COLUMN tg_webhook_secret text")

    op.execute("""
        CREATE TABLE message_queue (
            id bigserial PRIMARY KEY,
            clinic_id uuid NOT NULL REFERENCES clinic(id),
            update_id bigint NOT NULL,
            tg_chat_id bigint NOT NULL,
            payload jsonb NOT NULL,
            status text NOT NULL DEFAULT 'pending',
            attempts int NOT NULL DEFAULT 0,
            claimed_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (clinic_id, update_id),
            CONSTRAINT message_queue_status_check
                CHECK (status IN ('pending', 'processing', 'done', 'failed'))
        )
    """)
    op.execute("""
        CREATE INDEX ix_queue_claim ON message_queue (clinic_id, tg_chat_id, update_id)
        WHERE status IN ('pending', 'processing')
    """)

    op.execute("ALTER TABLE message_queue ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE message_queue FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON message_queue
        USING (clinic_id = current_setting('app.clinic_id')::uuid)
        WITH CHECK (clinic_id = current_setting('app.clinic_id')::uuid)
    """)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON message_queue TO navbat_app")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE message_queue_id_seq TO navbat_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS message_queue CASCADE")
    op.execute("ALTER TABLE clinic DROP COLUMN IF EXISTS tg_webhook_secret")
    op.execute("ALTER TABLE clinic DROP COLUMN IF EXISTS tg_admin_chat_id")
    op.execute("ALTER TABLE clinic DROP COLUMN IF EXISTS tg_bot_token_encrypted")
