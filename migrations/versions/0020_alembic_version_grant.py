"""GRANT SELECT ON alembic_version для navbat_app: --check сверяет ревизию
БД с head миграций.

Живая находка 12.06: база на 0018 при коде с 0019 проходила «[OK] БД и
миграции» (проверка щупала только таблицу 0006), а бот падал на
отсутствующей waitlist. Сверка с head требует читать alembic_version
из-под непривилегированной роли приложения.

Revision ID: 0020
"""
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON alembic_version TO navbat_app")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON alembic_version FROM navbat_app")
