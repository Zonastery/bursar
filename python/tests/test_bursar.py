from dataclasses import dataclass
from importlib.metadata import version
from typing import Any, cast, get_type_hints

import pytest

from bursar import (
    CapabilityNotConfiguredError,
    CatalogNotLoadedError,
    CommerceNotConfiguredError,
    ConfigError,
    CreditsCapability,
)
from bursar.billing.billing_service import BillingServiceOptions
from bursar.billing.billing_store import BillingStore
from bursar.billing.types import BillingEvent
from bursar.bursar import AccountService, Bursar, CatalogService
from bursar.commerce.types import CommerceOptions
from bursar.credits.service import CreditsService
from bursar.credits.store import CreditStore


def test_package_version_comes_from_installed_distribution_metadata() -> None:
    import bursar
    from bursar.credits import CreditsService as FocusedCreditsService

    assert bursar.__version__ == version("bursar")
    assert len(bursar.__all__) == len(set(bursar.__all__))
    assert not hasattr(bursar, "CommerceProviderRegistry")
    assert not hasattr(bursar, "CreditsService")
    assert FocusedCreditsService is CreditsService


def test_bursar_credits_exposes_only_the_curated_capability() -> None:
    expected_operations = {
        "add_credits",
        "aggregate_stats",
        "begin_billed_operation",
        "can_afford",
        "check_allowance",
        "check_feature",
        "daily_spend",
        "deduct",
        "deduct_credits",
        "deduct_flat_job",
        "deduct_team",
        "execute_grant_program",
        "get_available",
        "get_balance",
        "get_bucket_balances",
        "get_ledger_entry",
        "get_quota_state",
        "get_user_plan",
        "grant_subscription_cycle",
        "list_ledger_entries",
        "list_quota_events",
        "list_usage_charges",
        "list_usage_entries",
        "migrate_plan_batch",
        "record_usage",
        "refund_credits",
        "release",
        "renew",
        "reserve",
        "revoke_credits_by_entry_type",
        "run_billed",
        "resume_billed_operation",
        "set_user_plan",
        "settle",
        "spend_by_model",
        "spend_by_user",
        "start_plan_migration",
        "sweep_expired_credits",
        "top_users",
        "unset_user_plan",
    }

    assert get_type_hints(Bursar)["credits"] is CreditsCapability
    assert {
        name for name, value in CreditsCapability.__dict__.items() if callable(value) and not name.startswith("_")
    } == (expected_operations)


@dataclass
class FakeCredits:
    def load_catalog_from_store(self):
        return None

    def get_active_catalog(self):
        return {"version": 1}

    def publish_catalog_draft(self, config, label=None):
        return "draft-1"

    def activate_catalog_revision(self, version):
        return str(version)

    def publish_and_activate_catalog(self, config, label=None):
        return "revision-1"

    def apply_due_plan_changes(self, limit=100):
        return limit


def test_bursar_create_owns_catalog_and_delegates():
    credits = FakeCredits()
    catalog = CatalogService(cast(CreditsService, credits))

    assert catalog.get_active() == {"version": 1}
    assert catalog.publish_and_activate({"version": 1}) == "revision-1"
    assert catalog.apply_due_changes(7) == 7


def test_bursar_always_owns_billing_provisioning(monkeypatch):
    captured = {}

    class FakeBilling:
        def __init__(self, store, options, *, provisioning):
            captured["provisioning"] = provisioning
            captured["auto_select_entitlement_source"] = options.auto_select_entitlement_source

    monkeypatch.setattr("bursar.bursar.BillingEventService", FakeBilling)
    credits = FakeCredits()
    monkeypatch.setattr("bursar.bursar.CreditsServiceImpl", lambda **_kwargs: credits)
    Bursar(
        credit_store=cast(CreditStore, object()),
        billing_store=cast(BillingStore, object()),
        billing_options=BillingServiceOptions(auto_select_entitlement_source=False),
    )

    assert captured["provisioning"] is credits
    assert captured["auto_select_entitlement_source"] is False


@pytest.mark.parametrize(
    "options",
    [
        {"billing_options": BillingServiceOptions()},
        {"commerce_options": cast(Any, object())},
    ],
)
def test_bursar_rejects_options_for_unconfigured_capabilities(options: dict[str, Any]) -> None:
    with pytest.raises(ConfigError, match="requires billing_store"):
        Bursar(credit_store=cast(CreditStore, object()), **options)


def test_bursar_rejects_provider_environment_drift() -> None:
    class TestCreditStore:
        provider_environment = "live"

    class TestBillingStore:
        provider_environment = "test"

    async def provider_factory(_context):
        raise AssertionError("provider must not be loaded for invalid configuration")

    with pytest.raises(ConfigError, match="provider environments must match"):
        Bursar(
            credit_store=cast(CreditStore, TestCreditStore()),
            billing_store=cast(BillingStore, TestBillingStore()),
            commerce_options=CommerceOptions(
                providers={"mock": provider_factory},
                provider_environment="test",
            ),
        )


def test_bursar_routes_provider_events_through_billing_service(monkeypatch):
    class FakeBilling:
        def ingest_billing_event(self, event):
            return {"handled": True, "action": event["event_type"]}

    monkeypatch.setattr("bursar.bursar.CreditsServiceImpl", lambda **_kwargs: FakeCredits())
    monkeypatch.setattr("bursar.bursar.BillingEventService", lambda *_args, **_kwargs: FakeBilling())
    bursar = Bursar(
        credit_store=cast(CreditStore, object()),
        billing_store=cast(BillingStore, object()),
    )
    event = {"event_type": "subscription.created"}

    assert bursar.ingest_billing_event(cast(BillingEvent, event)) == {"handled": True, "action": "subscription.created"}


def test_bursar_public_facade_emits_typed_errors(monkeypatch):
    class EmptyCredits:
        def load_catalog_from_store(self):
            return None

        def get_active_catalog(self):
            return None

        def get_user_plan(self, _account_id):
            raise AssertionError("must not be called without a valid catalog")

    empty_credits = cast(Any, EmptyCredits())
    monkeypatch.setattr("bursar.bursar.CreditsServiceImpl", lambda **_kwargs: empty_credits)
    bursar = Bursar(credit_store=cast(CreditStore, object()))

    with pytest.raises(CatalogNotLoadedError):
        bursar.catalog.get_config()
    with pytest.raises(CapabilityNotConfiguredError):
        bursar.ingest_billing_event(cast(Any, {}))
    with pytest.raises(CapabilityNotConfiguredError):
        bursar.require_billing()
    with pytest.raises(CommerceNotConfiguredError):
        bursar.require_commerce()

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

    accounts = AccountService(empty_credits, cast(Any, EmptyCatalog()))
    with pytest.raises(ConfigError):
        accounts.on_account_created("user-1", "signup:user-1")
