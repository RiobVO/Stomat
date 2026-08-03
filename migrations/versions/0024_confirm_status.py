"""Статус подтверждения визита: пациент нажал «Приду» в напоминании.

Тап по кнопке был пустым ответом — клиника не узнавала, кто подтвердил, и
утром обзванивала всех подряд. Хранится только подтверждение: «молчит» не
состояние записи, а вывод (напоминание ушло, отметки нет) — иначе пришлось
бы держать её в актуальном виде фоновым процессом.

CHECK разрешает ровно одно значение: новый статус («не придёт») обязан
приехать миграцией вместе со своим обработчиком, а не опечаткой в UPDATE.
NULL проходит — это «ещё не подтверждал».

Revision ID: 0024
"""
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE appointment
            ADD COLUMN confirm_status text,
            ADD COLUMN confirmed_at timestamptz,
            ADD CONSTRAINT appointment_confirm_status_check
                CHECK (confirm_status IN ('confirmed'))
    """)


def downgrade() -> None:
    # constraint уходит вместе со своей колонкой — отдельного DROP не требует
    op.execute("ALTER TABLE appointment DROP COLUMN IF EXISTS confirm_status")
    op.execute("ALTER TABLE appointment DROP COLUMN IF EXISTS confirmed_at")
