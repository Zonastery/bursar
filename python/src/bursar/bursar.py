from __future__ import annotations

from dataclasses import dataclass

from bursar.billing.manager import BillingManager
from bursar.billing.store import BillingStore
from bursar.events import CreditEventEmitter
from bursar.interface.base import CreditStore
from bursar.manager import CreditManager


@dataclass(slots=True)
class CatalogService:
    """Catalog operations; billing never owns configuration writes."""

    credits: CreditManager

    def active(self):
        return self.credits.get_active_pricing()

    def publish_draft(self, config: dict, label: str | None = None) -> str:
        return self.credits.publish_pricing_draft(config, label)

    def activate(self, version: int) -> str:
        return self.credits.activate_pricing(version)

    def publish_and_activate(self, config: dict, label: str | None = None) -> None:
        self.credits.publish_pricing(config, label)


@dataclass(slots=True)
class Bursar:
    """Single application-facing boundary for credit and billing operations.

    The facade owns the lifecycle of both managers and prevents application
    code from wiring an unrelated credit manager into billing by accident.
    New integrations should depend on ``bursar.credits`` and
    ``bursar.billing`` rather than constructing either manager independently.
    """

    credits: CreditManager
    catalog: CatalogService
    billing: BillingManager | None = None

    @classmethod
    def create(
        cls,
        *,
        credit_store: CreditStore,
        billing_store: BillingStore | None = None,
        credit_manager: CreditManager | None = None,
        credit_manager_options: dict | None = None,
        billing_manager_options: dict | None = None,
        emitter: CreditEventEmitter | None = None,
    ) -> Bursar:
        credits = credit_manager or CreditManager(
            store=credit_store,
            emitter=emitter,
            **(credit_manager_options or {}),
        )
        billing = (
            BillingManager(
                billing_store,
                provisioning=credits,
                **(billing_manager_options or {}),
            )
            if billing_store is not None
            else None
        )
        return cls(credits=credits, billing=billing, catalog=CatalogService(credits))

    def setup(self):
        """Run the core database setup migrations."""
        return self.credits.setup()

    def load_catalog(self) -> None:
        """Load the active catalog into the metering engine."""
        self.credits.load_pricing_from_store()
