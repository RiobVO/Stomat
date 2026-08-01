"""C-2: webhook-секрет шифруется как токен бота.

tg_webhook_secret лежал открытым текстом — лишняя поверхность при
компрометации БД и несимметрично с tg_bot_token_encrypted. Backfill
перешифровывает существующие секреты; для этого миграции нужен
NAVBAT_ENC_KEY (тот же, под которым живёт клиника).

Revision ID: 0012
"""
import os

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE clinic ADD COLUMN tg_webhook_secret_encrypted text")
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, tg_webhook_secret FROM clinic "
        "WHERE tg_webhook_secret IS NOT NULL"
    )).all()
    if rows:
        if not os.environ.get("NAVBAT_ENC_KEY"):
            raise RuntimeError(
                "0012: NAVBAT_ENC_KEY обязателен — есть webhook-секреты "
                "для перешифрования")
        from navbat.crypto import encrypt_text
        for row in rows:
            conn.execute(
                sa.text("UPDATE clinic SET tg_webhook_secret_encrypted = :v "
                        "WHERE id = :id"),
                {"v": encrypt_text(row.tg_webhook_secret), "id": row.id})
    op.execute("ALTER TABLE clinic DROP COLUMN tg_webhook_secret")


def downgrade() -> None:
    # секреты при даунгрейде не восстанавливаем открытым текстом —
    # они перегенерируются onboard'ом при следующей записи токена
    op.execute("ALTER TABLE clinic ADD COLUMN tg_webhook_secret text")
    op.execute("ALTER TABLE clinic DROP COLUMN IF EXISTS tg_webhook_secret_encrypted")
