"""Self-service инкремент 2: деактивация врачей и услуг.

«Удалить врача/услугу» при наличии записей невозможно (FK RESTRICT на
appointment/waitlist), поэтому «удаление» = деактивация. is_active=false:
скрыт из записи и слотов, прошлые/будущие записи и /stats целы.
DEFAULT true — существующие строки остаются активными (бэкфилл не нужен).

Revision ID: 0021
"""
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE doctor  ADD COLUMN is_active boolean NOT NULL DEFAULT true")
    op.execute("ALTER TABLE service ADD COLUMN is_active boolean NOT NULL DEFAULT true")


def downgrade() -> None:
    op.execute("ALTER TABLE service DROP COLUMN IF EXISTS is_active")
    op.execute("ALTER TABLE doctor  DROP COLUMN IF EXISTS is_active")
