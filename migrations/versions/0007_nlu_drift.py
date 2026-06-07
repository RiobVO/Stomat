"""Ф1.5 B.3: метрика NLU-дрифта — сбои и repair-попытки в дневном учёте.

failures — ExtractionError после repair (деградация модели/промпта),
repairs — повторные попытки из-за невалидного JSON. Доля failures/requests
за день — сигнал дрифта, алерт админу считает UsageRecorder.

Revision ID: 0007
"""
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE llm_usage "
               "ADD COLUMN failures int NOT NULL DEFAULT 0, "
               "ADD COLUMN repairs int NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE llm_usage "
               "DROP COLUMN IF EXISTS failures, "
               "DROP COLUMN IF EXISTS repairs")
