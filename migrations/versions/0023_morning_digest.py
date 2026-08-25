"""Отметка утренней сводки: день, за который она уже ушла.

Вечерний дайджест держит свою отметку в clinic.last_digest_date (0006);
утренняя сводка «что у нас сегодня» — отдельное событие того же цикла и
своей отметки требует, иначе одна гасила бы другую. Колонка едет вместе
с лентой записей: схема меняется один раз на весь инкремент.

Revision ID: 0023
"""
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE clinic ADD COLUMN last_morning_digest_date date")


def downgrade() -> None:
    op.execute("ALTER TABLE clinic DROP COLUMN IF EXISTS last_morning_digest_date")
