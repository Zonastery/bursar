from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from bursar.billing.types import (
    BillingCustomerInfo,
    BillingEvent,
    BillingEventType,
    BillingSubscriptionChangeInput,
    BillingSubscriptionInfo,
    BillingSubscriptionState,
)
from bursar.commerce.errors import (
    ActiveSubscriptionError,
    CheckoutCompletedError,
    CheckoutConflictError,
    CommerceResourceNotFoundError,
    CoreBillingDataUnavailableError,
    InvalidOfferQuantityError,
    MissingPaymentMethodError,
    ProviderCapabilityNotSupportedError,
    QuoteChangedError,
    UnknownOfferError,
)
from bursar.commerce.plan_change import classify_subscription_change
from bursar.commerce.provider_registry import CommerceProviderRegistry
from bursar.commerce.types import (
    AccountCommerceOverview,
    AccountCreditOverview,
    AutoRechargeInput,
    BillingDocumentInvoiceRef,
    BillingDocumentLedgerRef,
    BillingDocumentRef,
    CheckoutStatusResult,
    CommerceOptions,
    CommerceSectionAvailability,
    CommerceWebhookResult,
    ConfirmPlanChangeResult,
    CreateCheckoutInput,
    CreateCheckoutResult,
    PlanChangePreviewResult,
    SubscriptionCommandResult,
)
from bursar.config import (
    CustomObjectReference,
    DodoProductReference,
    StripePriceReference,
    SubscriptionOffer,
    TopupOffer,
    load_config_from_dict,
)
from bursar.config.types import BursarConfig, CommerceOffer, SubscriptionChangePolicy
from bursar.providers.types import (
    ChangePlanLineItem,
    ChangePlanParams,
    ChangePlanPreview,
    CheckoutParams,
    PaymentMethodSetupParams,
    PaymentProvider,
    PortalParams,
    PreviewChangePlanParams,
    UpdatePaymentMethodParams,
    WebhookRequest,
)

_TERMINAL_CHECKOUT_STATUSES = {
    "failed",
    "cancelled",
    "requires_payment_method",
}
_DEFAULT_PREFERENCES = {
    "auto_recharge": False,
    "overage_protection": True,
    "email_notifications": True,
    "usage_alerts": True,
    "invoice_reminders": False,
}


def _external_id(reference: Any) -> str:
    if isinstance(reference, StripePriceReference):
        return reference.price_id
    if isinstance(reference, DodoProductReference):
        return reference.product_id
    if isinstance(reference, CustomObjectReference):
        return reference.external_id
    raise UnknownOfferError("Unsupported provider reference")


def _supports(provider: PaymentProvider, capability: str) -> bool:
    method = getattr(provider, capability, None)
    if not callable(method):
        return False
    base = getattr(PaymentProvider, capability, None)
    implementation = getattr(type(provider), capability, None)
    return implementation is not base


def _replace_intent(url: str, intent_id: str) -> str:
    return url.replace("{intentId}", intent_id)


def _status_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _line_item_dict(item: ChangePlanLineItem) -> dict[str, Any]:
    return {
        "productId": item.product_id,
        "unitPrice": item.unit_price,
        "quantity": item.quantity,
        "prorationFactor": item.proration_factor,
        "currency": item.currency.upper(),
        "tax": item.tax,
        "subtotal": item.subtotal,
    }


def _quote_fingerprint(preview: ChangePlanPreview) -> str:
    financial = {
        "totalAmount": preview.total_amount,
        "settlementAmount": preview.settlement_amount,
        "currency": preview.currency.upper(),
        "recurringAmount": preview.recurring_amount,
        "recurringCurrency": (preview.recurring_currency.upper() if preview.recurring_currency is not None else None),
        "taxAmount": preview.tax_amount,
        "customerCredits": preview.customer_credits,
        "lineItems": [_line_item_dict(item) for item in (preview.line_items or [])],
    }
    encoded = json.dumps(financial, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _plan_change_provider_args(
    policy: SubscriptionChangePolicy,
) -> tuple[str, str]:
    return (
        "next_billing_date" if policy.effective == "renewal" else "immediately",
        "do_not_bill" if policy.proration == "none" else "prorated_immediately",
    )


class CommerceAutoRecharge:
    def __init__(self, commerce: CommerceService) -> None:
        self._commerce = commerce

    async def get_status(self, account_id: str):
        provider = await self._commerce.provider_for_account(account_id)
        return await self._commerce.billing.auto_recharge.get_status(
            account_id,
            provider,
        )

    async def enable(self, input: AutoRechargeInput):
        provider = await self._commerce.provider_for_account(input.account_id)
        balance = self._commerce.credits.get_balance(input.account_id)
        try:
            return await self._commerce.billing.auto_recharge.enable(
                input.account_id,
                provider,
                balance=balance.balance,
                return_url=input.return_url,
            )
        except ValueError as exc:
            if "payment_method" in str(exc):
                raise MissingPaymentMethodError() from exc
            raise

    def disable(self, account_id: str) -> None:
        self._commerce.billing.auto_recharge.disable(account_id)

    async def retry(self, input: AutoRechargeInput):
        provider = await self._commerce.provider_for_account(input.account_id)
        balance = self._commerce.credits.get_balance(input.account_id)
        try:
            await self._commerce.billing.auto_recharge.retry(
                input.account_id,
                provider,
                balance=balance.balance,
                return_url=input.return_url,
            )
            return await self._commerce.billing.auto_recharge.get_status(
                input.account_id,
                provider,
            )
        except ValueError as exc:
            if "payment_method" in str(exc):
                raise MissingPaymentMethodError() from exc
            raise

    async def process_if_needed(self, input: AutoRechargeInput):
        profile = self._commerce.billing.get_auto_recharge_profile(input.account_id)
        if profile is None or not profile.enabled or profile.state != "active":
            from bursar.billing.auto_recharge_service import AutoRechargeProcessResult

            return AutoRechargeProcessResult(outcome="disabled")
        provider = (
            await self._commerce.providers.get(profile.provider)
            if profile.provider
            else await self._commerce.provider_for_account(input.account_id)
        )
        balance = self._commerce.credits.get_balance(input.account_id)
        return await self._commerce.billing.auto_recharge.process_if_needed(
            input.account_id,
            provider,
            balance=balance.balance,
            return_url=input.return_url,
        )


class CommerceService:
    """Framework-independent catalog, billing-state, and provider coordinator."""

    def __init__(
        self,
        billing: Any,
        credits: Any,
        event_sink: Any,
        options: CommerceOptions,
    ) -> None:
        self.billing = billing
        self.credits = credits
        self.options = options
        self.providers = CommerceProviderRegistry(options, event_sink)
        self.auto_recharge = CommerceAutoRecharge(self)

    async def get_provider(self, provider_name: str) -> PaymentProvider:
        return await self.providers.get(provider_name)

    def clear_provider_cache(self) -> None:
        self.providers.clear()

    def _active_config(self) -> BursarConfig:
        raw = self.billing.get_active_bursar_config()
        if raw is None:
            raise CoreBillingDataUnavailableError("The active commerce catalog is unavailable")
        try:
            return load_config_from_dict(raw)
        except Exception as exc:
            raise CoreBillingDataUnavailableError("The active commerce catalog is invalid") from exc

    def _assert_offer_type(
        self,
        offer: CommerceOffer,
        checkout_type: str | None,
    ) -> None:
        if checkout_type is None:
            return
        expected = TopupOffer if checkout_type == "credit_pack" else SubscriptionOffer
        if not isinstance(offer, expected):
            raise UnknownOfferError(f"Offer is not a {checkout_type} offer")

    def _resolve_offer(
        self,
        *,
        offer_key: str,
        checkout_type: str | None = None,
    ) -> tuple[BursarConfig, str, CommerceOffer]:
        config = self._active_config()
        offer = config.commerce.offers.get(offer_key)
        if offer is None:
            raise UnknownOfferError(f"Unknown offer {offer_key!r}")
        self._assert_offer_type(offer, checkout_type)
        return config, offer_key, offer

    @staticmethod
    def _quantity(offer: CommerceOffer, requested: int | None) -> int:
        if isinstance(offer, SubscriptionOffer):
            if requested is not None and requested != 1:
                raise InvalidOfferQuantityError(
                    "Subscription quantity must be 1",
                    1,
                    1,
                )
            return 1
        quantity = requested if requested is not None else offer.quantity.default
        if quantity < offer.quantity.minimum:
            raise InvalidOfferQuantityError(
                f"Minimum quantity is {offer.quantity.minimum}",
                offer.quantity.minimum,
                offer.quantity.maximum,
            )
        if quantity > offer.quantity.maximum:
            raise InvalidOfferQuantityError(
                f"Maximum quantity is {offer.quantity.maximum}",
                offer.quantity.minimum,
                offer.quantity.maximum,
            )
        return quantity

    async def provider_for_account(
        self,
        account_id: str,
        offer: CommerceOffer | None = None,
    ) -> PaymentProvider:
        subscription = self.billing.get_user_subscription(account_id)
        customer = self.billing.get_customer_by_user_id(
            account_id,
            subscription.provider if subscription else None,
        )
        return await self.providers.select(
            current=(subscription.provider if subscription else customer.provider if customer else None),
            offer=offer,
        )

    async def create_checkout(
        self,
        input: CreateCheckoutInput,
    ) -> CreateCheckoutResult:
        _config, offer_key, offer = self._resolve_offer(
            offer_key=input.offer_key,
            checkout_type=input.type,
        )
        quantity = self._quantity(offer, input.quantity)
        blocking = self.billing.get_blocking_subscription(input.account_id) if input.account_id else None
        customer = self.billing.get_customer_by_user_id(input.account_id) if input.account_id else None
        if isinstance(offer, SubscriptionOffer) and blocking is not None:
            raise ActiveSubscriptionError("The account already has a blocking subscription")
        provider = await self.providers.select(
            requested=input.provider,
            current=customer.provider if customer else None,
            offer=offer,
        )
        reference = offer.providers.get(provider.provider)
        if reference is None:
            raise UnknownOfferError("Offer has no reference for the selected provider")

        provider_customer = (
            self.billing.get_customer_by_user_id(
                input.account_id,
                provider.provider,
            )
            if input.account_id
            else None
        )
        metadata = dict(input.metadata)
        if input.account_id:
            metadata["userId"] = input.account_id
        if isinstance(offer, SubscriptionOffer):
            metadata["plan_slug"] = offer.plan
            metadata["billing_interval"] = offer.billing_interval.unit
            checkout_kind = "subscription"
        else:
            metadata["credits"] = str(offer.credits_per_unit * quantity)
            metadata["quantity"] = str(quantity)
            checkout_kind = "credit_topup"

        digest_value = {
            "checkoutKind": "subscription" if checkout_kind == "subscription" else "topup",
            "offerKey": offer_key,
            "provider": provider.provider,
            "quantity": quantity,
        }
        request_digest = hashlib.sha256(json.dumps(digest_value, separators=(",", ":")).encode()).hexdigest()

        def create_intent():
            return self.billing.create_or_get_checkout_intent(
                input.subject_id,
                provider.provider,
                checkout_kind,
                offer_key,
                request_digest,
                (datetime.now(UTC) + timedelta(seconds=self.options.checkout_intent_ttl_seconds)).isoformat(),
            )

        intent = create_intent()
        if intent.request_digest != request_digest:
            raise CheckoutConflictError("A checkout is already in progress for a different offer")
        if intent.checkout_url:
            locally_expired = datetime.fromisoformat(intent.expires_at) <= datetime.now(UTC)
            if not locally_expired and (
                not intent.provider_session_id or not _supports(provider, "get_checkout_session_status")
            ):
                raise CheckoutConflictError(
                    "A checkout is already in progress; continue it in the existing checkout window"
                )
            state = (
                None
                if locally_expired
                else await provider.get_checkout_session_status(cast(str, intent.provider_session_id))
            )
            payment_status = state.get("paymentStatus") if state else None
            if payment_status == "succeeded":
                self.billing.update_checkout_intent(
                    intent.id,
                    status="completed",
                )
                raise CheckoutCompletedError()
            if not locally_expired and state is not None and payment_status not in _TERMINAL_CHECKOUT_STATUSES:
                raise CheckoutConflictError(
                    "A checkout is already in progress; continue it in the existing checkout window"
                )
            self.billing.update_checkout_intent(
                intent.id,
                status="expired" if state is None else "failed",
            )
            intent = create_intent()

        try:
            session = await provider.create_checkout_session(
                CheckoutParams(
                    user_id=input.account_id,
                    customer_id=(provider_customer.provider_customer_id if provider_customer else None),
                    email=input.email,
                    product_id=_external_id(reference),
                    type=("subscription" if isinstance(offer, SubscriptionOffer) else "credit_pack"),
                    quantity=quantity,
                    return_url=_replace_intent(input.return_url, intent.id),
                    cancel_url=_replace_intent(input.cancel_url, intent.id),
                    metadata={**metadata, "checkout_intent_id": intent.id},
                    idempotency_key=input.operation_key,
                )
            )
            self.billing.update_checkout_intent(
                intent.id,
                provider_session_id=session.get("providerSessionId"),
                checkout_url=session["url"],
            )
            customer_id = session.get("customerId")
            if (
                input.account_id
                and customer_id
                and (provider_customer is None or customer_id != provider_customer.provider_customer_id)
            ):
                self.billing.upsert_customer(
                    provider.provider,
                    customer_id,
                    input.account_id,
                    input.email,
                )
            return CreateCheckoutResult(
                intent_id=intent.id,
                url=session["url"],
                provider=provider.provider,
                offer_key=offer_key,
            )
        except Exception:
            self.billing.update_checkout_intent(intent.id, status="failed")
            raise

    def get_checkout_status(
        self,
        intent_id: str,
        subject_id: str,
    ) -> CheckoutStatusResult:
        intent = self.billing.get_checkout_intent(intent_id, subject_id)
        if intent is None:
            raise CommerceResourceNotFoundError("Checkout intent not found")
        status = _status_value(intent.status)
        expired = status == "open" and datetime.fromisoformat(intent.expires_at) <= datetime.now(UTC)
        mapped = (
            "expired"
            if expired
            else "pending"
            if status == "open"
            else "succeeded"
            if status == "completed"
            else status
        )
        return CheckoutStatusResult(
            intent_id=intent.id,
            status=cast(Any, mapped),
        )

    async def cancel_subscription(
        self,
        account_id: str,
        operation_key: str,
    ) -> SubscriptionCommandResult:
        subscription = self.billing.get_user_subscription(account_id)
        if subscription is None or not subscription.provider_subscription_id:
            raise CommerceResourceNotFoundError("No active subscription found")
        if _status_value(subscription.status) not in {
            "active",
            "trialing",
            "past_due",
        }:
            return SubscriptionCommandResult()
        provider = await self.providers.get(subscription.provider)
        if not _supports(provider, "cancel_subscription"):
            raise ProviderCapabilityNotSupportedError(
                provider.provider,
                "cancel_subscription",
            )
        await provider.cancel_subscription(
            subscription.provider_subscription_id,
            operation_key,
        )
        self.billing.ingest_billing_event(
            BillingEvent(
                provider=subscription.provider,
                event_id=f"cancel_{account_id}_{operation_key}",
                event_type=BillingEventType.subscription_cancellation_scheduled,
                occurred_at=datetime.now(UTC).isoformat(),
                user_id=account_id,
                customer=BillingCustomerInfo(provider_customer_id=subscription.provider_customer_id),
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id=subscription.provider_subscription_id,
                    cancel_at_period_end=True,
                ),
            )
        )
        return SubscriptionCommandResult(pending=True)

    async def reactivate_subscription(
        self,
        account_id: str,
        operation_key: str,
    ) -> SubscriptionCommandResult:
        subscription = self.billing.get_user_subscription(account_id)
        if subscription is None or not subscription.provider_subscription_id:
            raise CommerceResourceNotFoundError("No subscription found")
        status = _status_value(subscription.status)
        if (status == "active" and not subscription.cancel_at_period_end) or (
            not subscription.cancel_at_period_end and status != "canceled"
        ):
            return SubscriptionCommandResult()
        provider = await self.providers.get(subscription.provider)
        if not _supports(provider, "reactivate_subscription"):
            raise ProviderCapabilityNotSupportedError(
                provider.provider,
                "reactivate_subscription",
            )
        await provider.reactivate_subscription(
            subscription.provider_subscription_id,
            operation_key,
        )
        self.billing.ingest_billing_event(
            BillingEvent(
                provider=subscription.provider,
                event_id=f"reactivate_{account_id}_{operation_key}",
                event_type=BillingEventType.subscription_cancellation_unscheduled,
                occurred_at=datetime.now(UTC).isoformat(),
                user_id=account_id,
                customer=BillingCustomerInfo(provider_customer_id=subscription.provider_customer_id),
                subscription=BillingSubscriptionInfo(
                    provider_subscription_id=subscription.provider_subscription_id,
                    cancel_at_period_end=False,
                ),
            )
        )
        return SubscriptionCommandResult(pending=True)

    async def _plan_context(
        self,
        account_id: str,
        offer_key: str,
    ) -> dict[str, Any]:
        subscription = self.billing.get_active_subscription(account_id)
        entitlement = self.credits.get_user_plan(account_id)
        config, _key, offer = self._resolve_offer(
            offer_key=offer_key,
            checkout_type="subscription",
        )
        if subscription is None or not subscription.provider_subscription_id:
            raise CommerceResourceNotFoundError("No active subscription found")
        if not isinstance(offer, SubscriptionOffer):
            raise UnknownOfferError()
        provider = await self.providers.select(
            requested=subscription.provider,
            current=subscription.provider,
            offer=offer,
        )
        reference = offer.providers.get(provider.provider)
        if reference is None:
            raise UnknownOfferError("Target offer is unavailable from the provider")
        if isinstance(reference, StripePriceReference):
            persisted = self.billing.resolve_offer(
                provider.provider,
                None,
                reference.price_id,
            )
        elif isinstance(reference, DodoProductReference):
            persisted = self.billing.resolve_offer(
                provider.provider,
                reference.product_id,
                None,
            )
        else:
            persisted = self.billing.resolve_offer_by_lookup(
                provider.provider,
                reference.external_id,
            )
        if persisted is None:
            raise CommerceResourceNotFoundError("Target offer is not present in persisted billing state")
        current_plan = entitlement.plan_key or subscription.plan
        if not current_plan:
            raise CommerceResourceNotFoundError("Current subscription plan is unknown")
        classified = classify_subscription_change(
            config,
            current_plan,
            subscription.interval,
            offer,
        )
        return {
            "subscription": subscription,
            "provider": provider,
            "persisted": persisted,
            "offer": offer,
            "product_id": _external_id(reference),
            "classification": classified.classification,
            "policy": classified.policy,
            "target_interval": classified.target_interval,
        }

    async def _refresh_plan_preview(
        self,
        context: dict[str, Any],
    ) -> ChangePlanPreview:
        provider = cast(PaymentProvider, context["provider"])
        if not _supports(provider, "preview_change_plan"):
            raise ProviderCapabilityNotSupportedError(
                provider.provider,
                "preview_change_plan",
            )
        effective_at, proration = _plan_change_provider_args(context["policy"])
        return await provider.preview_change_plan(
            PreviewChangePlanParams(
                provider_subscription_id=context["subscription"].provider_subscription_id,
                product_id=context["product_id"],
                effective_at=effective_at,
                proration_billing_mode=proration,
            )
        )

    async def preview_plan_change(
        self,
        account_id: str,
        *,
        offer_key: str,
    ) -> PlanChangePreviewResult:
        context = await self._plan_context(account_id, offer_key)
        offer = cast(SubscriptionOffer, context["offer"])
        if context["classification"] == "unchanged":
            return PlanChangePreviewResult(
                unchanged=True,
                classification="unchanged",
                scheduled=False,
                plan_id=offer.plan,
                interval=context["target_interval"],
            )
        preview = await self._refresh_plan_preview(context)
        return PlanChangePreviewResult(
            unchanged=False,
            classification=context["classification"],
            scheduled=context["policy"].effective == "renewal",
            plan_id=offer.plan,
            interval=context["target_interval"],
            preview=preview,
            quote_fingerprint=_quote_fingerprint(preview),
        )

    async def confirm_plan_change(
        self,
        account_id: str,
        operation_key: str,
        *,
        offer_key: str,
        quote_fingerprint: str,
    ) -> ConfirmPlanChangeResult:
        context = await self._plan_context(account_id, offer_key)
        offer = cast(SubscriptionOffer, context["offer"])
        if context["classification"] == "unchanged":
            return ConfirmPlanChangeResult(
                success=True,
                unchanged=True,
                plan_id=offer.plan,
                interval=context["target_interval"],
            )
        preview = await self._refresh_plan_preview(context)
        refreshed = PlanChangePreviewResult(
            unchanged=False,
            classification=context["classification"],
            scheduled=context["policy"].effective == "renewal",
            plan_id=offer.plan,
            interval=context["target_interval"],
            preview=preview,
            quote_fingerprint=_quote_fingerprint(preview),
        )
        if quote_fingerprint != refreshed.quote_fingerprint:
            raise QuoteChangedError(refreshed)
        provider = cast(PaymentProvider, context["provider"])
        if not _supports(provider, "change_plan"):
            raise ProviderCapabilityNotSupportedError(
                provider.provider,
                "change_plan",
            )
        subscription = cast(BillingSubscriptionState, context["subscription"])
        existing = self.billing.get_open_billing_subscription_change(
            subscription.provider,
            subscription.provider_subscription_id,
        )
        if existing is not None and existing.state == "scheduled" and existing.proration_behavior == "none":
            if not _supports(provider, "cancel_scheduled_plan_change"):
                raise ProviderCapabilityNotSupportedError(
                    provider.provider,
                    "cancel_scheduled_plan_change",
                )
            await provider.cancel_scheduled_plan_change(
                subscription.provider_subscription_id,
                existing.provider_operation_id,
                f"{operation_key}:replace",
            )
            self.billing.update_billing_subscription_change(
                existing.id,
                state="canceled",
            )
        elif existing is not None:
            raise CheckoutConflictError("A plan change is already awaiting payment")

        scheduled = context["policy"].effective == "renewal"
        effective_at = preview.next_billing_date if scheduled else preview.effective_at or datetime.now(UTC).isoformat()
        if not effective_at:
            raise CoreBillingDataUnavailableError("The provider did not return the scheduled change date")
        change = self.billing.create_billing_subscription_change(
            BillingSubscriptionChangeInput(
                provider=subscription.provider,
                provider_subscription_id=subscription.provider_subscription_id,
                to_offer_id=context["persisted"].offer_id,
                effective_at=effective_at,
                idempotency_key=operation_key,
                proration_behavior=("none" if context["policy"].proration == "none" else "invoice_immediately"),
            )
        )
        try:
            if subscription.cancel_at_period_end:
                if not _supports(provider, "reactivate_subscription"):
                    raise ProviderCapabilityNotSupportedError(
                        provider.provider,
                        "reactivate_subscription",
                    )
                await provider.reactivate_subscription(
                    subscription.provider_subscription_id,
                    f"{operation_key}:keep",
                )
            provider_effective, proration = _plan_change_provider_args(context["policy"])
            result = await provider.change_plan(
                ChangePlanParams(
                    provider_subscription_id=subscription.provider_subscription_id,
                    product_id=context["product_id"],
                    effective_at=provider_effective,
                    proration_billing_mode=proration,
                    on_payment_failure=context["policy"].payment_failure,
                    metadata={
                        "userId": account_id,
                        "plan_slug": offer.plan,
                        "billing_interval": context["target_interval"],
                    },
                    idempotency_key=operation_key,
                )
            )
            operation_id = result.get("providerOperationId") if isinstance(result, dict) else None
            self.billing.update_billing_subscription_change(
                change.id,
                provider_operation_id=operation_id,
            )
        except Exception as exc:
            self.billing.update_billing_subscription_change(
                change.id,
                state="failed",
                error_message=str(exc),
            )
            raise
        return ConfirmPlanChangeResult(
            success=True,
            pending=True,
            scheduled=scheduled,
            effective_at=effective_at,
            plan_id=offer.plan,
            interval=context["target_interval"],
        )

    async def cancel_scheduled_plan_change(
        self,
        account_id: str,
        operation_key: str,
    ) -> dict[str, bool]:
        subscription = self.billing.get_active_subscription(account_id)
        if subscription is None:
            raise CommerceResourceNotFoundError("No active subscription found")
        change = self.billing.get_open_billing_subscription_change(
            subscription.provider,
            subscription.provider_subscription_id,
        )
        if change is None or change.state != "scheduled" or change.proration_behavior != "none":
            raise CommerceResourceNotFoundError("No scheduled plan change found")
        provider = await self.providers.get(subscription.provider)
        if not _supports(provider, "cancel_scheduled_plan_change"):
            raise ProviderCapabilityNotSupportedError(
                provider.provider,
                "cancel_scheduled_plan_change",
            )
        await provider.cancel_scheduled_plan_change(
            subscription.provider_subscription_id,
            change.provider_operation_id,
            operation_key,
        )
        self.billing.update_billing_subscription_change(
            change.id,
            state="canceled",
        )
        return {"success": True}

    async def create_portal_session(
        self,
        account_id: str,
        return_url: str,
        *,
        purpose: Literal["billing", "payment-method"] = "billing",
        cancel_url: str | None = None,
    ) -> dict:
        subscription = self.billing.get_user_subscription(account_id)
        customer = self.billing.get_customer_by_user_id(
            account_id,
            subscription.provider if subscription else None,
        )
        if customer is None or not customer.provider_customer_id:
            raise CommerceResourceNotFoundError("No billing customer found")
        provider = await self.providers.get(customer.provider)
        if purpose == "payment-method":
            if subscription is not None and subscription.provider_subscription_id:
                if not _supports(
                    provider,
                    "create_update_payment_method_session",
                ):
                    raise ProviderCapabilityNotSupportedError(
                        provider.provider,
                        "create_update_payment_method_session",
                    )
                return await provider.create_update_payment_method_session(
                    UpdatePaymentMethodParams(
                        customer_id=customer.provider_customer_id,
                        subscription_id=subscription.provider_subscription_id,
                        return_url=return_url,
                    )
                )
            if not _supports(
                provider,
                "create_payment_method_setup_session",
            ):
                raise ProviderCapabilityNotSupportedError(
                    provider.provider,
                    "create_payment_method_setup_session",
                )
            return await provider.create_payment_method_setup_session(
                PaymentMethodSetupParams(
                    customer_id=customer.provider_customer_id,
                    return_url=return_url,
                    cancel_url=cancel_url,
                )
            )
        if not _supports(provider, "create_customer_portal_session"):
            raise ProviderCapabilityNotSupportedError(
                provider.provider,
                "create_customer_portal_session",
            )
        return await provider.create_customer_portal_session(
            PortalParams(
                customer_id=customer.provider_customer_id,
                return_url=return_url,
            )
        )

    def _preferences(
        self,
        account_id: str,
        current: Any,
    ):
        values = {
            **_DEFAULT_PREFERENCES,
            **self.options.preference_defaults,
        }
        if current is not None:
            values.update(current.model_dump(exclude={"user_id"}))
        from bursar.billing.types import BillingPreferences

        return BillingPreferences(user_id=account_id, **values)

    def update_preferences(
        self,
        account_id: str,
        patch: dict[str, bool],
    ):
        current = self.billing.get_user_preferences(account_id)
        next_preferences = self._preferences(account_id, current)
        next_preferences = next_preferences.model_copy(update=patch)
        self.billing.update_user_preferences(next_preferences)
        return next_preferences

    @staticmethod
    def _ledger_document(entry: Any) -> BillingDocumentLedgerRef | None:
        metadata = entry.metadata or {}
        provider = metadata.get("provider") if isinstance(metadata.get("provider"), str) else None
        document_id = next(
            (
                metadata.get(key)
                for key in (
                    "provider_document_id",
                    "provider_invoice_id",
                    "provider_payment_id",
                )
                if isinstance(metadata.get(key), str)
            ),
            None,
        )
        legacy = metadata.get("dodo_payment_id") if isinstance(metadata.get("dodo_payment_id"), str) else None
        if not document_id and not legacy:
            return None
        return BillingDocumentLedgerRef(
            kind="ledger_entry",
            ledger_entry_id=entry.entry_id,
            provider=provider or ("dodo" if legacy else None),
            provider_document_id=document_id or legacy,
            created_at=entry.created_at,
            entry_type=entry.entry_type,
            amount=entry.amount,
        )

    async def get_account_overview(
        self,
        account_id: str,
    ) -> AccountCommerceOverview:
        try:
            balance = self.credits.get_balance(account_id)
            available = self.credits.get_available(account_id)
            buckets = self.credits.get_bucket_balances(account_id)
            entitlement = self.credits.get_user_plan(account_id)
            allowance = self.credits.check_allowance(account_id)
            subscription = self.billing.get_user_subscription(account_id)
            preferences = self._preferences(
                account_id,
                self.billing.get_user_preferences(account_id),
            )
        except Exception as exc:
            raise CoreBillingDataUnavailableError("Core credit state is unavailable") from exc

        pending_change = (
            self.billing.get_open_billing_subscription_change(
                subscription.provider,
                subscription.provider_subscription_id,
            )
            if subscription
            else None
        )
        transactions_available = usage_available = documents_available = True
        try:
            transactions = [
                entry
                for entry in self.credits.list_ledger_entries(
                    account_id,
                    limit=50,
                ).items
                if entry.entry_type != "usage"
            ]
        except Exception:
            transactions_available = False
            transactions = []
        try:
            usage = self.credits.list_usage_entries(
                account_id,
                limit=100,
            ).items
        except Exception:
            usage_available = False
            usage = []
        try:
            invoices = self.billing.list_billing_invoices(account_id)
        except Exception:
            documents_available = False
            invoices = []

        documents: list[BillingDocumentRef] = [
            BillingDocumentInvoiceRef(
                kind="provider_invoice",
                provider=invoice.provider or (subscription.provider if subscription else ""),
                provider_document_id=invoice.provider_invoice_id,
                status=invoice.status,
                amount_paid_minor=invoice.amount_paid_minor,
                amount_due_minor=invoice.amount_due_minor,
                currency=invoice.currency,
                period_start=invoice.period_start,
                period_end=invoice.period_end,
            )
            for invoice in invoices
            if invoice.provider or subscription
        ]
        documents.extend(document for entry in transactions if (document := self._ledger_document(entry)) is not None)

        payment_methods: list[Any] = []
        payment_methods_available = True
        auto_recharge = None
        auto_recharge_available = True
        try:
            customer = self.billing.get_customer_by_user_id(
                account_id,
                subscription.provider if subscription else None,
            )
            if customer is not None:
                provider = await self.providers.get(customer.provider)
                if _supports(provider, "list_payment_methods"):
                    payment_methods = await provider.list_payment_methods(customer.provider_customer_id)
                else:
                    payment_methods_available = False
                try:
                    auto_recharge = await self.billing.auto_recharge.get_status(
                        account_id,
                        provider,
                    )
                except Exception:
                    auto_recharge_available = False
        except Exception:
            payment_methods_available = False
            auto_recharge_available = False

        return AccountCommerceOverview(
            account_id=account_id,
            credits=AccountCreditOverview(
                ledger_balance=balance.balance,
                effective_spendable_balance=available.available,
                lifetime_purchases=balance.lifetime_purchased,
                allowance_remaining=allowance.allowance_remaining,
                allowance_limit=entitlement.allowance_amount,
                allowance_period_start=allowance.period_start,
                allowance_period_end=allowance.period_end,
                buckets=buckets.buckets,
            ),
            entitlement=entitlement,
            subscription=subscription,
            pending_change=pending_change,
            preferences=preferences,
            payment_methods=payment_methods,
            documents=documents,
            provider_invoices=invoices,
            transactions=transactions,
            usage=usage,
            auto_recharge=auto_recharge,
            availability=CommerceSectionAvailability(
                payment_methods=payment_methods_available,
                documents=documents_available and transactions_available,
                transactions=transactions_available,
                usage=usage_available,
                auto_recharge=auto_recharge_available,
            ),
        )

    async def get_invoice_link(
        self,
        account_id: str,
        document: BillingDocumentRef,
    ) -> dict[str, str]:
        provider_name: str
        provider_document_id: str
        if document.kind == "provider_invoice":
            owned = next(
                (
                    invoice
                    for invoice in self.billing.list_billing_invoices(account_id)
                    if (invoice.provider or document.provider) == document.provider
                    and invoice.provider_invoice_id == document.provider_document_id
                ),
                None,
            )
            if owned is None:
                raise CommerceResourceNotFoundError("Invoice not found")
            provider_name = document.provider
            provider_document_id = owned.provider_invoice_id
        else:
            entry = self.credits.get_ledger_entry(
                account_id,
                document.ledger_entry_id,
            )
            if entry is None:
                raise CommerceResourceNotFoundError("Ledger entry not found")
            owned = self._ledger_document(entry)
            if owned is None or owned.provider is None or owned.provider_document_id is None:
                raise CommerceResourceNotFoundError("No provider document is associated with the ledger entry")
            provider_name = owned.provider
            provider_document_id = owned.provider_document_id
        provider = await self.providers.get(provider_name)
        if not _supports(provider, "get_invoice_url"):
            raise ProviderCapabilityNotSupportedError(
                provider.provider,
                "get_invoice_url",
            )
        result = await provider.get_invoice_url(provider_document_id)
        if result is None:
            raise CommerceResourceNotFoundError("No invoice URL is available")
        return result

    async def handle_webhook(
        self,
        *,
        raw_body: str,
        headers: dict[str, str],
        provider: str | None = None,
    ) -> CommerceWebhookResult:
        selected = await self.providers.select(requested=provider)
        result = await selected.handle_webhook(WebhookRequest(raw_body=raw_body, headers=headers))
        return CommerceWebhookResult.from_provider(result)
