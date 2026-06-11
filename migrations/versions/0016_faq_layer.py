"""П-2б: FAQ-слой — адрес клиники + копилка неотвеченных вопросов.

clinic.address — ответ на «где вы находитесь?» без админа (NULL = бот
честно отвечает «не понял»). unanswered_question — анонимная копилка
вопросов без ответа (телефоны маскируются ДО записи, chat_id не храним):
владелец видит спрос в вечернем дайджесте, retention чистит вместе с
диалогами.

Revision ID: 0016
"""
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE clinic ADD COLUMN address text")
    op.execute("""
        CREATE TABLE unanswered_question (
            id bigserial PRIMARY KEY,
            clinic_id uuid NOT NULL REFERENCES clinic(id),
            question text NOT NULL,
            at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_unanswered_question_day "
               "ON unanswered_question (clinic_id, at)")
    op.execute("ALTER TABLE unanswered_question ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE unanswered_question FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON unanswered_question
        USING (clinic_id = current_setting('app.clinic_id')::uuid)
        WITH CHECK (clinic_id = current_setting('app.clinic_id')::uuid)
    """)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON unanswered_question "
               "TO navbat_app")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE unanswered_question_id_seq "
               "TO navbat_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS unanswered_question")
    op.execute("ALTER TABLE clinic DROP COLUMN IF EXISTS address")
