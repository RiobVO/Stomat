"""Полировка-3: телефон пациента шифрованным (пересмотр решения 11.06).

patient.phone_encrypted — AES-256-GCM-шифртекст номера (тот же NAVBAT_ENC_KEY,
паритет с name_encrypted): владельцу нужен номер в событии Google Calendar.
contact_hash остаётся ключом поиска/дедупа. NULL у существующих пациентов —
штатно: строка телефона в событии просто пропускается, backfill не нужен.

Revision ID: 0018
"""
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE patient ADD COLUMN phone_encrypted text")


def downgrade() -> None:
    op.execute("ALTER TABLE patient DROP COLUMN IF EXISTS phone_encrypted")
