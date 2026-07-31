"""C-2: метка последнего успешного синка календаря.

/health должен отличать «синк живёт» от «синк тихо умер»: цикл синка
штампует поле при каждом успешном прогоне, health сравнивает возраст
с интервалом синка.

Revision ID: 0011
"""
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE clinic ADD COLUMN gcal_last_sync_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE clinic DROP COLUMN IF EXISTS gcal_last_sync_at")
