"""C-3: момент завершения обработки — для метрики p95 ответа.

BRIEF SLA: ответ p95 < 5 с. created_at → completed_at покрывает весь путь
пациентского сообщения: ожидание в очереди + FSM + отправка ответа.

Revision ID: 0013
"""
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE message_queue ADD COLUMN completed_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE message_queue DROP COLUMN IF EXISTS completed_at")
