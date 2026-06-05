"""Инкремент 2: таблица conversation — состояние диалога FSM.

fsm_state + context (jsonb) переживают рестарт процесса; один разговор
на чат в пределах клиники (UNIQUE clinic_id, tg_chat_id). RLS — как в 0001.

Revision ID: 0002
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE conversation (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            clinic_id uuid NOT NULL REFERENCES clinic(id),
            tg_chat_id bigint NOT NULL,
            patient_id uuid REFERENCES patient(id),
            fsm_state text NOT NULL DEFAULT 'idle',
            context jsonb NOT NULL DEFAULT '{}',
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (clinic_id, tg_chat_id)
        )
    """)
    op.execute("ALTER TABLE conversation ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE conversation FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON conversation
        USING (clinic_id = current_setting('app.clinic_id')::uuid)
        WITH CHECK (clinic_id = current_setting('app.clinic_id')::uuid)
    """)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON conversation TO navbat_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS conversation CASCADE")
