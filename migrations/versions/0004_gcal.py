"""Инкремент 4: Google Calendar — реквизиты синхронизации.

Календарь на врача (gcal_calendar_id), incremental sync через syncToken,
связь запись↔событие — appointment.gcal_event_id (уникальна в пределах
клиники: одно событие = одна запись).

Revision ID: 0004
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE clinic ADD COLUMN gcal_refresh_token_encrypted text")
    op.execute("ALTER TABLE doctor ADD COLUMN gcal_calendar_id text")
    op.execute("ALTER TABLE doctor ADD COLUMN gcal_sync_token text")
    op.execute("ALTER TABLE appointment ADD COLUMN gcal_event_id text")
    op.execute("""
        CREATE UNIQUE INDEX ux_appointment_gcal_event
        ON appointment (clinic_id, gcal_event_id)
        WHERE gcal_event_id IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_appointment_gcal_event")
    op.execute("ALTER TABLE appointment DROP COLUMN IF EXISTS gcal_event_id")
    op.execute("ALTER TABLE doctor DROP COLUMN IF EXISTS gcal_sync_token")
    op.execute("ALTER TABLE doctor DROP COLUMN IF EXISTS gcal_calendar_id")
    op.execute("ALTER TABLE clinic DROP COLUMN IF EXISTS gcal_refresh_token_encrypted")
