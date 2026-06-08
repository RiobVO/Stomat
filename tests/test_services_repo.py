"""Слой данных каталога услуг (services_repo) — прямое покрытие после
вынесения SQL из FSM (R1a)."""
from __future__ import annotations

from conftest import make_service
from navbat.db.base import tenant_transaction
from navbat.dialog import services_repo


def test_service_id_name_roundtrip(app_session_factory, clinic_a, service_cleaning):
    with tenant_transaction(app_session_factory, clinic_a) as s:
        sid = services_repo.service_id(s, "cleaning")
        assert sid == service_cleaning
        assert services_repo.service_name(s, sid) == "cleaning"
        assert services_repo.service_id(s, "no_such_service") is None


def test_service_keys_sorted(app_session_factory, clinic_a, admin_engine,
                             service_cleaning):
    make_service(admin_engine, clinic_a, "xray", 15)
    with tenant_transaction(app_session_factory, clinic_a) as s:
        keys = services_repo.service_keys(s)
    assert keys == sorted(keys)
    assert {"cleaning", "xray"} <= set(keys)


def test_price_list_and_price(app_session_factory, clinic_a, admin_engine):
    make_service(admin_engine, clinic_a, "checkup", 30, price=150_000)
    make_service(admin_engine, clinic_a, "implant", 90, price=None)
    with tenant_transaction(app_session_factory, clinic_a) as s:
        rows = services_repo.price_list(s)
        prices = {r.name: r.price for r in rows}
        assert prices["checkup"] == 150_000
        assert prices["implant"] is None
        assert services_repo.service_price(s, "checkup") == 150_000
        assert services_repo.service_price(s, "no_such_service") is None
