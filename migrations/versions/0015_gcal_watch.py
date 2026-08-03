"""C-6: push-каналы Google Calendar (events.watch) per врач.

Канал у Google не продлевается — открывается новый заранее до expiration;
id/resource/expiration храним, чтобы продлевать и валидировать входящие
push-уведомления по channel_id в URL.

Revision ID: 0015
"""
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE doctor ADD COLUMN gcal_channel_id text")
    op.execute("ALTER TABLE doctor ADD COLUMN gcal_resource_id text")
    op.execute("ALTER TABLE doctor ADD COLUMN gcal_channel_expires_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE doctor DROP COLUMN IF EXISTS gcal_channel_id")
    op.execute("ALTER TABLE doctor DROP COLUMN IF EXISTS gcal_resource_id")
    op.execute("ALTER TABLE doctor DROP COLUMN IF EXISTS gcal_channel_expires_at")
