"""--check сверяет ревизию БД с head миграций (живая находка 12.06:
база на 0018 при коде с 0019 проходила «[OK] БД и миграции», а бот
падал на отсутствующей waitlist каждые 30 секунд)."""
from __future__ import annotations

from sqlalchemy import text

from navbat.db.base import tenant_transaction
from navbat.supervisor import check_migrations, migrations_head


def test_app_role_reads_alembic_version(app_session_factory, clinic_a):
    # без GRANT (0020) проверка миграций невозможна под navbat_app
    with tenant_transaction(app_session_factory, clinic_a) as session:
        rev = session.execute(
            text("SELECT version_num FROM alembic_version")).scalar_one()
    assert rev


def test_check_green_when_db_at_head(app_session_factory, clinic_a):
    ok, detail = check_migrations(app_session_factory, clinic_a)
    assert ok
    assert migrations_head() in detail


def test_check_red_when_db_behind(admin_engine, app_session_factory, clinic_a):
    head = migrations_head()
    with admin_engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = '0018'"))
    try:
        ok, detail = check_migrations(app_session_factory, clinic_a)
        assert not ok
        assert "0018" in detail and head in detail
        assert "alembic upgrade head" in detail, "подсказка обязана быть в строке"
    finally:
        with admin_engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = :v"),
                         {"v": head})
