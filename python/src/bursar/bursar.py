from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel

from bursar.billing.auto_recharge_service import AutoRechargeService
from bursar.billing.billing_service import BillingService as BillingEventService
from bursar.billing.billing_store import BillingStore
from bursar.billing.contracts import (
    AutoRechargeAttemptClaim,
    AutoRechargeAttemptUpdate,
    AutoRechargeProviderPaymentUpdate,
    BillingEventSink,
    BillingSubscriptionChangeUpdate,
    BillingSubscriptionConflictCreate,
    CheckoutIntentCreate,
    CheckoutIntentUpdate,
)
from bursar.billing.service_types import BillingServiceOptions
from bursar.billing.types import (
    BillingAutoRechargeAttempt,
    BillingAutoRechargeProfile,
    BillingCustomerRecord,
    BillingEvent,
    BillingEventResult,
    BillingInvoiceRecord,
    BillingOfferResult,
    BillingPreferences,
    BillingSubscriptionChange,
    BillingSubscriptionChangeInput,
    BillingSubscriptionState,
    BillingTopupResult,
    CheckoutIntent,
)
from bursar.catalog import PublicCatalog, project_public_catalog
from bursar.commerce.errors import CommerceNotConfiguredError
from bursar.commerce.service import CommerceService
from bursar.commerce.types import AutoRechargeInput, CommerceOptions
from bursar.config import BursarConfigData, CatalogRollout, ParsedBursarConfig, load_config_from_dict
from bursar.credits.events import CreditEventEmitter
from bursar.credits.service import (
    BilledOperation,
    RunBilledResult,
)
from bursar.credits.service import (
    CreditsService as CreditsServiceImpl,
)
from bursar.credits.service_types import (
    BeginBilledOperationOptions,
    CanAffordOptions,
    CreditsServiceOptions,
    ExactAmount,
    GrantSubscriptionCycleOptions,
    MetricsOrAmount,
    PostDeductionContext,
    ReserveOptions,
    RunBilledOptions,
    SettleOptions,
)
from bursar.credits.store import CreditStore
from bursar.credits.types import (
    AddCreditsResult,
    AggregateStats,
    AllowanceResult,
    AvailableResult,
    BalanceResult,
    BucketBalancesResult,
    CanAffordResult,
    CatalogRevision,
    CheckFeatureResult,
    CreditMetadata,
    DailySpendRow,
    DeductionResult,
    ExecuteGrantProgramRequest,
    GetUserPlanResult,
    GrantProgramAwardResult,
    LeaseResult,
    LedgerCursor,
    LedgerEntry,
    LedgerPage,
    ListQuotaEventsOptions,
    PlanMigrationBatchResult,
    PlanMigrationStartResult,
    QuotaEvent,
    QuotaState,
    RefundResult,
    ReleaseResult,
    RevokeCreditsResult,
    SetUserPlanResult,
    SpendByModelRow,
    SpendByUserRow,
    SweepResult,
    TeamDeductionResult,
    TopUserRow,
    UnsetUserPlanResult,
    UsageChargeCursor,
    UsageChargePage,
    UsageRecordResult,
)
from bursar.errors import CapabilityNotConfiguredError, CatalogNotLoadedError, ConfigError
from bursar.metrics import UsageMetrics
from bursar.retry import retry_bursar_operation


class CreditsCapability(Protocol):
    """Stable public credit capability exposed by the Bursar facade.

    Keeping this list explicit prevents implementation and catalog-management
    helpers added to ``CreditsService`` from silently becoming facade API.
    Applications that intentionally need the concrete service can construct it
    from :mod:`bursar.credits`.
    """

    def get_user_plan(self, user_id: str) -> GetUserPlanResult: ...

    def check_feature(self, user_id: str, feature: str) -> CheckFeatureResult: ...

    def get_quota_state(
        self,
        user_id: str,
        quota_key: str | None = None,
    ) -> list[QuotaState]: ...

    def list_quota_events(
        self,
        user_id: str,
        options: ListQuotaEventsOptions | None = None,
    ) -> list[QuotaEvent]: ...

    def start_plan_migration(
        self,
        from_plan_id: str | None,
        to_plan_id: str,
    ) -> PlanMigrationStartResult: ...

    def migrate_plan_batch(
        self,
        migration_id: str,
        batch_size: int = 100,
    ) -> PlanMigrationBatchResult: ...

    def revoke_credits_by_entry_type(
        self,
        user_id: str,
        entry_type: str,
    ) -> RevokeCreditsResult: ...

    def execute_grant_program(
        self,
        request: ExecuteGrantProgramRequest,
    ) -> list[GrantProgramAwardResult]: ...

    def get_ledger_entry(self, user_id: str, entry_id: str) -> LedgerEntry | None: ...

    def get_available(self, user_id: str) -> AvailableResult: ...

    def aggregate_stats(self, start: datetime, end: datetime) -> AggregateStats: ...

    def spend_by_user(self, start: datetime, end: datetime) -> list[SpendByUserRow]: ...

    def spend_by_model(self, start: datetime, end: datetime) -> list[SpendByModelRow]: ...

    def list_ledger_entries(
        self,
        user_id: str,
        *,
        entry_types: list[str] | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        limit: int = 50,
        cursor: LedgerCursor | None = None,
    ) -> LedgerPage: ...

    def list_usage_entries(
        self,
        user_id: str,
        *,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        limit: int = 50,
        cursor: LedgerCursor | None = None,
    ) -> LedgerPage: ...

    def list_usage_charges(
        self,
        user_id: str,
        *,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        limit: int = 50,
        cursor: UsageChargeCursor | None = None,
        include_record_only: bool = True,
    ) -> UsageChargePage: ...

    def top_users(self, limit: int, start: datetime, end: datetime) -> list[TopUserRow]: ...

    def daily_spend(self, start: datetime, end: datetime) -> list[DailySpendRow]: ...

    def set_user_plan(
        self,
        user_id: str,
        plan_key: str,
        *,
        plan_assigned_at: datetime | None = None,
    ) -> SetUserPlanResult: ...

    def unset_user_plan(self, user_id: str) -> UnsetUserPlanResult: ...

    def get_balance(self, user_id: str) -> BalanceResult: ...

    def add_credits(
        self,
        user_id: str,
        amount: ExactAmount,
        *,
        idempotency_key: str,
        entry_type: str = "adjustment",
        metadata: CreditMetadata | None = None,
        expires_at: datetime | None = None,
        bucket: str | None = None,
    ) -> AddCreditsResult: ...

    def deduct_credits(
        self,
        user_id: str,
        amount: ExactAmount,
        *,
        idempotency_key: str,
        entry_type: str = "adjustment",
        bucket: str | None = None,
        metadata: CreditMetadata | None = None,
    ) -> AddCreditsResult: ...

    def grant_subscription_cycle(
        self,
        user_id: str,
        amount: ExactAmount,
        options: GrantSubscriptionCycleOptions,
    ) -> AddCreditsResult: ...

    def reserve(
        self,
        user_id: str,
        metrics_or_amount: MetricsOrAmount,
        options: ReserveOptions,
    ) -> LeaseResult: ...

    def settle(
        self,
        user_id: str,
        lease_id: str,
        metrics_or_amount: MetricsOrAmount,
        options: SettleOptions | None = None,
    ) -> DeductionResult: ...

    def release(self, user_id: str, lease_id: str) -> ReleaseResult: ...

    def renew(self, user_id: str, lease_id: str, ttl: int | None = None) -> LeaseResult: ...

    def can_afford(
        self,
        user_id: str,
        metrics_or_amount: MetricsOrAmount,
        options: CanAffordOptions | None = None,
    ) -> CanAffordResult: ...

    def get_bucket_balances(self, user_id: str) -> BucketBalancesResult: ...

    def check_allowance(self, user_id: str) -> AllowanceResult | None: ...

    def run_billed(
        self,
        user_id: str,
        options: RunBilledOptions,
    ) -> RunBilledResult: ...

    def begin_billed_operation(
        self,
        user_id: str,
        options: BeginBilledOperationOptions,
    ) -> BilledOperation: ...

    def resume_billed_operation(
        self,
        user_id: str,
        lease_id: str,
        operation_key: str,
        *,
        feature: str | None = None,
        metadata: CreditMetadata | None = None,
    ) -> BilledOperation: ...

    def deduct(
        self,
        user_id: str,
        metrics: UsageMetrics,
        *,
        idempotency_key: str,
        metadata: CreditMetadata | None = None,
        feature: str | None = None,
    ) -> DeductionResult: ...

    def record_usage(
        self,
        user_id: str,
        metrics: UsageMetrics,
        *,
        idempotency_key: str,
        metadata: CreditMetadata | None = None,
    ) -> UsageRecordResult: ...

    def deduct_flat_job(
        self,
        user_id: str,
        job_name: str,
        *,
        idempotency_key: str,
        metadata: CreditMetadata | None = None,
        feature: str | None = None,
    ) -> DeductionResult: ...

    def refund_credits(
        self,
        entry_id: str,
        *,
        idempotency_key: str,
        amount: ExactAmount | None = None,
        reason: str | None = None,
        metadata: CreditMetadata | None = None,
    ) -> RefundResult: ...

    def deduct_team(
        self,
        team_id: str,
        user_id: str,
        metrics: UsageMetrics,
        *,
        idempotency_key: str,
        metadata: CreditMetadata | None = None,
    ) -> TeamDeductionResult: ...

    def sweep_expired_credits(self, dry_run: bool = False) -> SweepResult: ...


class BillingCapability(BillingEventSink, Protocol):
    """Public billing capability exposed by the Bursar facade."""

    auto_recharge: AutoRechargeService

    @property
    def has_provisioning(self) -> bool: ...

    def create_or_get_checkout_intent(
        self,
        input: CheckoutIntentCreate,
    ) -> CheckoutIntent: ...

    def update_checkout_intent(
        self,
        id: str,
        update: CheckoutIntentUpdate,
    ) -> None: ...

    def get_checkout_intent(self, id: str, subject_id: str) -> CheckoutIntent | None: ...

    def get_user_subscription(self, user_id: str) -> BillingSubscriptionState | None: ...

    def get_active_subscription(self, user_id: str) -> BillingSubscriptionState | None: ...

    def list_cancellable_provider_subscription_ids(self, user_id: str) -> list[str]: ...

    def list_cancellable_subscriptions(self, user_id: str) -> list[BillingSubscriptionState]: ...

    def get_blocking_subscription(self, user_id: str) -> BillingSubscriptionState | None: ...

    def get_user_preferences(self, user_id: str) -> BillingPreferences | None: ...

    def get_active_catalog_document(self) -> dict[str, Any] | None: ...

    def list_billing_invoices(self, user_id: str) -> list[BillingInvoiceRecord]: ...

    def create_billing_subscription_change(
        self,
        input: BillingSubscriptionChangeInput,
    ) -> BillingSubscriptionChange: ...

    def get_open_billing_subscription_change(
        self,
        provider: str,
        provider_subscription_id: str,
    ) -> BillingSubscriptionChange | None: ...

    def update_billing_subscription_change(
        self,
        id: str,
        update: BillingSubscriptionChangeUpdate,
    ) -> None: ...

    def record_subscription_conflict(
        self,
        input: BillingSubscriptionConflictCreate,
    ) -> None: ...

    def upsert_billing_subscription(self, state: BillingSubscriptionState) -> None: ...

    def update_user_preferences(self, prefs: BillingPreferences) -> None: ...

    def get_auto_recharge_profile(self, user_id: str) -> BillingAutoRechargeProfile | None: ...

    def upsert_auto_recharge_profile(
        self,
        profile: BillingAutoRechargeProfile,
        *,
        reset_cooldown: bool = False,
    ) -> None: ...

    def claim_auto_recharge_attempt(
        self,
        input: AutoRechargeAttemptClaim,
    ) -> BillingAutoRechargeAttempt | None: ...

    def update_auto_recharge_attempt(
        self,
        input: AutoRechargeAttemptUpdate,
    ) -> None: ...

    def update_auto_recharge_attempt_by_provider_payment(
        self,
        input: AutoRechargeProviderPaymentUpdate,
    ) -> None: ...

    def count_auto_recharge_attempts(
        self,
        user_id: str,
        since: str | datetime,
    ) -> int: ...

    def get_customer_by_user_id(self, user_id: str, provider: str | None = None) -> BillingCustomerRecord | None: ...

    def resolve_offer(
        self, provider: str, product_id: str | None = None, price_id: str | None = None
    ) -> BillingOfferResult | None: ...

    def resolve_offer_by_lookup(
        self,
        provider: str,
        lookup_key: str,
    ) -> BillingOfferResult | None: ...

    def resolve_topup(
        self, provider: str, product_id: str | None = None, price_id: str | None = None
    ) -> BillingTopupResult | None: ...

    def resolve_topup_by_lookup(
        self,
        provider: str,
        lookup_key: str,
    ) -> BillingTopupResult | None: ...

    def upsert_customer(
        self, provider: str, provider_customer_id: str, user_id: str, email: str | None = None
    ) -> None: ...

    def pseudonymize_financial_subject(self, user_id: str) -> None: ...

    def expire_past_due_grace_periods(
        self,
        now: datetime | None = None,
    ) -> int: ...

    def invalidate_offer_cache(self) -> None: ...


class CatalogService:
    """Catalog operations; billing never owns configuration writes."""

    def __init__(self, credits: CreditsServiceImpl) -> None:
        self._credits = credits

    def get_active(self) -> CatalogRevision | None:
        """Return the active persisted catalog revision, if one exists."""
        return self._credits.get_active_catalog()

    @property
    def is_loaded(self) -> bool:
        """Whether this process currently has a pricing engine loaded."""
        return self._credits.pricing_engine is not None

    def load(self) -> None:
        """Load the active persisted catalog into this process."""
        self._credits.load_catalog_from_store()

    def refresh(self) -> None:
        """Block until a stale in-process catalog has been refreshed."""
        self._credits.refresh_catalog_if_stale()

    def invalidate(self) -> None:
        """Force the next refresh to reload the active catalog."""
        self._credits.invalidate_catalog()

    def get_config(self) -> ParsedBursarConfig:
        active = self.get_active()
        if active is None:
            raise CatalogNotLoadedError("No active Bursar catalog is available")
        return load_config_from_dict(active.config)

    def public_view(self) -> PublicCatalog:
        return project_public_catalog(self.get_config())

    def publish_draft(self, config: BursarConfigData, label: str | None = None) -> str:
        return self._credits.publish_catalog_draft(config, label)

    def activate(
        self,
        version: int,
        rollout: CatalogRollout | dict[str, Any] | None = None,
    ) -> str:
        if rollout is None:
            return self._credits.activate_catalog_revision(version)
        rollout_data = rollout.model_dump(mode="json") if isinstance(rollout, CatalogRollout) else rollout
        return self._credits.activate_catalog_revision(version, rollout_data)

    def publish_and_activate(
        self,
        config: BursarConfigData,
        label: str | None = None,
        rollout: CatalogRollout | dict[str, Any] | None = None,
    ) -> str:
        if rollout is None:
            return self._credits.publish_and_activate_catalog(config, label)
        rollout_data = rollout.model_dump(mode="json") if isinstance(rollout, CatalogRollout) else rollout
        return self._credits.publish_and_activate_catalog(config, label, rollout_data)

    def set_revision_pin(self, user_id: str, pinned: bool) -> bool:
        """Pin or unpin one current assignment from automatic catalog rollout."""
        return self._credits.set_plan_revision_pin(user_id, pinned)

    def apply_due_changes(self, limit: int = 100) -> int:
        """Apply one bounded batch of renewal-effective plan changes."""
        return self._credits.apply_due_plan_changes(limit)


class AccountService:
    """Generic financial lifecycle operations for SaaS accounts."""

    def __init__(self, credits: CreditsServiceImpl, catalog: CatalogService) -> None:
        self._credits = credits
        self._catalog = catalog

    def on_account_created(
        self,
        account_id: str,
        event_key: str,
        *,
        region: str | None = None,
        metadata: CreditMetadata | None = None,
    ) -> AccountCreatedResult:
        from bursar.credits.types import ExecuteGrantProgramRequest

        if not event_key.strip():
            raise ConfigError("event_key must not be empty")
        config = retry_bursar_operation(self._catalog.get_config)
        plan_key = config.catalog.default_plan
        if plan_key is None:
            raise ConfigError("The active catalog has no default account plan")
        current = retry_bursar_operation(self._credits.get_user_plan, account_id)
        plan_assigned = current.plan_key is None
        if plan_assigned:
            retry_bursar_operation(self._credits.set_user_plan, account_id, plan_key)
        grants = []
        for program_key, program in sorted(config.credits.grant_programs.items()):
            if program.trigger != "account_created":
                continue
            grants.extend(
                retry_bursar_operation(
                    self._credits.execute_grant_program,
                    ExecuteGrantProgramRequest(
                        trigger="account_created",
                        program_key=program_key,
                        subject_id=account_id,
                        event_key=event_key,
                        region=region,
                        metadata=metadata,
                    ),
                )
            )
        return AccountCreatedResult(
            account_id=account_id,
            plan_key=current.plan_key or plan_key,
            plan_assigned=plan_assigned,
            grants=grants,
        )


class AccountCreatedResult(BaseModel):
    """Result of applying the account-created lifecycle exactly once."""

    account_id: str
    plan_key: str
    plan_assigned: bool
    grants: list[GrantProgramAwardResult]


class Bursar:
    """Single application-facing boundary for credit and billing operations.

    The facade owns the lifecycle of both services and prevents application
    code from wiring unrelated credit and billing implementations together.
    Integrations should depend on ``bursar.credits`` and ``bursar.billing``
    rather than constructing lifecycle services independently.
    """

    credits: CreditsCapability
    catalog: CatalogService
    accounts: AccountService
    billing: BillingCapability | None
    commerce: CommerceService | None

    def __init__(
        self,
        *,
        credit_store: CreditStore,
        billing_store: BillingStore | None = None,
        credits_options: CreditsServiceOptions | None = None,
        billing_options: BillingServiceOptions | None = None,
        commerce_options: CommerceOptions | None = None,
        emitter: CreditEventEmitter | None = None,
    ) -> None:
        if billing_store is None and billing_options is not None:
            raise ConfigError("billing_options requires billing_store")
        if billing_store is None and commerce_options is not None:
            raise ConfigError("commerce_options requires billing_store")
        environments = {
            name: environment
            for name, environment in (
                ("credit_store", getattr(credit_store, "provider_environment", None)),
                ("billing_store", getattr(billing_store, "provider_environment", None)),
                (
                    "commerce_options",
                    commerce_options.provider_environment if commerce_options is not None else None,
                ),
            )
            if environment is not None
        }
        if len(set(environments.values())) > 1:
            configured = ", ".join(f"{name}={environment}" for name, environment in environments.items())
            raise ConfigError(f"Bursar provider environments must match: {configured}")
        credits = CreditsServiceImpl(
            store=credit_store,
            emitter=emitter,
            options=credits_options,
        )
        self.credits = credits
        self.catalog = CatalogService(credits)
        self.accounts = AccountService(credits, self.catalog)
        self.billing = (
            BillingEventService(
                billing_store,
                billing_options,
                provisioning=credits,
            )
            if billing_store is not None
            else None
        )
        self.commerce = (
            CommerceService(
                self.billing,
                credits,
                self,
                commerce_options,
            )
            if self.billing is not None and commerce_options is not None
            else None
        )
        commerce = self.commerce
        if commerce is not None:

            async def process_auto_recharge(context: PostDeductionContext) -> None:
                await commerce.auto_recharge.process_if_needed(AutoRechargeInput(account_id=context.user_id))

            credits.add_post_deduction_hook(process_auto_recharge)

    def load_catalog(self) -> None:
        """Load the active catalog into the pricing engine."""
        self.catalog.load()

    def require_billing(self) -> BillingCapability:
        """Return billing or raise the SDK's typed configuration error."""
        if self.billing is None:
            raise CapabilityNotConfiguredError("billing")
        return self.billing

    def require_commerce(self) -> CommerceService:
        """Return commerce or raise the SDK's typed configuration error."""
        if self.commerce is None:
            raise CommerceNotConfiguredError("Bursar commerce capability is not configured")
        return self.commerce

    def ingest_billing_event(self, event: BillingEvent) -> BillingEventResult:
        """Submit a normalized provider event through the facade."""
        return self.require_billing().ingest_billing_event(event)
