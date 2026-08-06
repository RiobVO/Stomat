"""Полировка-2: FAQ-темы оплата и телефон клиники.

clinic.payment_info — ответ на «можно картой / рассрочка есть?»,
clinic.phone — на «какой у вас номер?» (NULL = бот честно отвечает
«не понял», вопрос копится в unanswered_question). Зеркало
clinic.address из 0016.

Revision ID: 0017
"""
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE clinic ADD COLUMN payment_info text")
    op.execute("ALTER TABLE clinic ADD COLUMN phone text")


def downgrade() -> None:
    op.execute("ALTER TABLE clinic DROP COLUMN IF EXISTS phone")
    op.execute("ALTER TABLE clinic DROP COLUMN IF EXISTS payment_info")
