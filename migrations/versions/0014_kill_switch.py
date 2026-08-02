"""C-4: kill-switch — пауза бота и выключение LLM на клинику.

bot_paused: воркер отвечает пациентам «временно по телефону», команды
админа работают. llm_enabled=false: кнопочные сценарии живут, свободный
текст уходит в меню без вызова LLM и без эскалаций.

Revision ID: 0014
"""
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE clinic ADD COLUMN bot_paused boolean "
               "NOT NULL DEFAULT false")
    op.execute("ALTER TABLE clinic ADD COLUMN llm_enabled boolean "
               "NOT NULL DEFAULT true")


def downgrade() -> None:
    op.execute("ALTER TABLE clinic DROP COLUMN IF EXISTS bot_paused")
    op.execute("ALTER TABLE clinic DROP COLUMN IF EXISTS llm_enabled")
