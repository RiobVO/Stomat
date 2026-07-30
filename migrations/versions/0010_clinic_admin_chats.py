"""M4: несколько админ-чатов на клинику.

Один tg_admin_chat_id запирал управление и алерты на единственный Telegram-
аккаунт (ресепшен ⊕ владелец ⊕ смены — кто-то оставался без сигналов и без
команд). Массив tg_admin_chat_ids — источник истины для авторизации команд
(/release, /forget и пр.) и веера алертов; старый столбец остаётся для
совместимости отображения.

Revision ID: 0010
"""
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE clinic ADD COLUMN tg_admin_chat_ids bigint[] "
               "NOT NULL DEFAULT '{}'")
    op.execute("UPDATE clinic SET tg_admin_chat_ids = ARRAY[tg_admin_chat_id] "
               "WHERE tg_admin_chat_id IS NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE clinic DROP COLUMN IF EXISTS tg_admin_chat_ids")
