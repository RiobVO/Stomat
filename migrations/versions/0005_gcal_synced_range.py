"""Снапшот времени, отправленного в GCal.

Экспорт-пас находит изменённые записи сравнением time_range с
gcal_synced_range — без чтения событий из Google (квота) и без
дополнительных колонок-таймстампов.

Revision ID: 0005
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE appointment ADD COLUMN gcal_synced_range tstzrange")


def downgrade() -> None:
    op.execute("ALTER TABLE appointment DROP COLUMN IF EXISTS gcal_synced_range")
