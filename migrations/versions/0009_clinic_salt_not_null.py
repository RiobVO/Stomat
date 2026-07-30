"""B3: соль клиники обязательна.

Хэш телефона пациента = SHA-256(phone + clinic.salt). Пустая/общая соль
делает узбекские номера (~10^9) обратимыми перебором при компрометации БД.
Создание клиники теперь генерирует криптослучайную соль (onboard.create_clinic);
здесь добиваем возможные NULL и запрещаем NULL на уровне схемы.

Revision ID: 0009
"""
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # md5(random())x2 = 64 hex-символа; без расширений (gen_random_bytes не нужен)
    op.execute("UPDATE clinic SET salt = md5(random()::text) || md5(random()::text) "
               "WHERE salt IS NULL OR salt = ''")
    op.execute("ALTER TABLE clinic ALTER COLUMN salt SET NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE clinic ALTER COLUMN salt DROP NOT NULL")
