from dataclasses import dataclass
from typing import Any, cast

import pytest

from bursar import CommerceNotConfiguredError, ConfigError, PricingNotLoadedError
from bursar.bursar import Bursar


@dataclass
class FakeCredits:
    def load_pricing_from_store(self):
        return None

    def get_active_pricing(self):
        return {"version": 1}

    def publish_pricing_draft(self, config, label=None):
        return "draft-1"

    def activate_pricing(self, version):
        return str(version)

    def publish_pricing(self, config, label=None):
        return None


def test_bursar_create_owns_catalog_and_delegates():
    credits = FakeCredits()
    bursar = Bursar.create(credit_store=object(), credits=credits)

    assert bursar.credits is credits
    assert bursar.billing is None
    assert bursar.catalog.active == {"version": 1}


def test_bursar_always_owns_billing_provisioning(monkeypatch):
    captured = {}

    class FakeBilling:
        def __init__(self, store, options):
            captured["provisioning"] = options.provisioning
            captured["auto_select_entitlement_source"] = options.auto_select_entitlement_source

    monkeypatch.setattr("bursar.bursar.BillingEventService", FakeBilling)
    credits = FakeCredits()
    Bursar.create(
        credit_store=object(),
        billing_store=object(),
        credits=credits,
        billing_options={"auto_select_entitlement_source": False},
    )

    assert captured["provisioning"] is credits
    assert captured["auto_select_entitlement_source"] is False


def test_bursar_routes_provider_events_through_billing_service():
    class FakeBilling:
        def ingest_billing_event(self, event):
            return {"handled": True, "action": event["event_type"]}

    bursar = Bursar(credits=FakeCredits(), catalog=None, billing=FakeBilling())
    event = {"event_type": "subscription.created"}

    assert bursar.ingest_billing_event(event) == {"handled": True, "action": "subscription.created"}


def test_bursar_public_facade_emits_typed_errors():
    class EmptyCredits:
        def load_pricing_from_store(self):
            return None

        def get_active_pricing(self):
            return None

        def get_user_plan(self, _account_id):
            raise AssertionError("must not be called without a valid catalog")

    bursar = Bursar(credits=cast(Any, EmptyCredits()))

    with pytest.raises(PricingNotLoadedError):
        bursar.catalog.get_config()
    with pytest.raises(CommerceNotConfiguredError):
        bursar.ingest_billing_event(cast(Any, {}))

    class EmptyCatalog:
        def get_config(self):
            return type(
                "Config",
                (),
                {
                    "catalog": type("Catalog", (), {"default_plan": None})(),
                    "plans": {},
                    "credits": type("Credits", (), {"grant_programs": {}})(),
                },
            )()

    bursar.accounts._catalog = cast(Any, EmptyCatalog())
    with pytest.raises(ConfigError):
        bursar.accounts.on_account_created("user-1", "signup:user-1")
