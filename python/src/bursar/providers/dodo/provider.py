from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any

from bursar.bursar import BillingEventSink
from bursar.errors import ProviderResponseError
from bursar.providers.dodo.event_mapper import dodo_billing_event_id, handle_dodo_billing_event
from bursar.providers.types import (
    ChangePlanLineItem,
    ChangePlanParams,
    ChangePlanPreview,
    ChangePlanResult,
    CheckoutParams,
    CheckoutSessionResult,
    CheckoutSessionStatus,
    CreateCustomerParams,
    CreateCustomerResult,
    PaymentMethodInfo,
    PaymentMethodSetupParams,
    PortalParams,
    PreviewChangePlanParams,
    ProviderLogger,
    ProviderUrlResult,
    SavedPaymentChargeParams,
    SavedPaymentChargeQuote,
    SavedPaymentChargeResult,
    UpdatePaymentMethodParams,
    WebhookRequest,
    WebhookResult,
    deduplicate_payment_methods,
    normalize_provider_logger,
)
from bursar.shared.diagnostics import persisted_diagnostic_summary
from bursar.shared.idempotency import require_stable_key
from bursar.shared.numbers import MAX_SAFE_INTEGER

if TYPE_CHECKING:
    from dodopayments import AsyncDodoPayments

logger = logging.getLogger(__name__)

_BURSAR_METADATA_KEYS = {
    "bursar_account_id",
    "plan_slug",
    "billing_interval",
    "credits",
    "checkout_intent_id",
}


def _normalize_metadata(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Dodo webhook metadata must be an object")
    normalized: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError("Dodo webhook metadata keys must be strings")
        if not isinstance(item, str | int | float | bool) or (isinstance(item, float) and not math.isfinite(item)):
            raise ValueError(f"Dodo webhook metadata.{key} must be a scalar value")
        if key in _BURSAR_METADATA_KEYS and not isinstance(item, str):
            raise ValueError(f"Dodo webhook metadata.{key} must be a string")
        normalized[key] = str(item)
    return normalized


def _idempotency_headers(key: str) -> dict[str, str]:
    return {"Idempotency-Key": require_stable_key(key)}


def _require_provider_text(value: object, operation: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderResponseError("dodo", operation, details={"field": field})
    return value


def _require_provider_int(
    value: object,
    operation: str,
    field: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool):
        raise ProviderResponseError("dodo", operation, details={"field": field})
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        parsed = int(value)
    else:
        raise ProviderResponseError("dodo", operation, details={"field": field})
    upper_bound = MAX_SAFE_INTEGER if maximum is None else min(maximum, MAX_SAFE_INTEGER)
    if parsed < minimum or parsed > upper_bound:
        raise ProviderResponseError("dodo", operation, details={"field": field})
    return parsed


def _require_provider_number(value: object, operation: str, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        raise ProviderResponseError("dodo", operation, details={"field": field})
    return float(value)


def _require_provider_instant(value: object, operation: str, field: str) -> str:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise ProviderResponseError("dodo", operation, cause=error, details={"field": field}) from error
    if parsed.utcoffset() is None:
        raise ProviderResponseError("dodo", operation, details={"field": field})
    return parsed.isoformat()


class DodoProvider:
    provider = "dodo"

    def __init__(
        self,
        *,
        get_client: Callable[[], AsyncDodoPayments],
        webhook_key: str,
        event_sink: BillingEventSink,
        setup_product_id: str | None = None,
        logger: ProviderLogger | None = None,
    ) -> None:
        if not webhook_key.strip():
            raise ValueError("webhook_key must not be empty")
        self._get_client = get_client
        self._webhook_key = webhook_key
        self._setup_product_id = setup_product_id
        self._sink = event_sink
        self._logger = normalize_provider_logger(logger)

    async def create_checkout_session(self, params: CheckoutParams) -> CheckoutSessionResult:
        if not params.account_id:
            raise ValueError("Authentication required for checkout")
        client = self._get_client()
        quantity = params.quantity if params.quantity is not None else 1
        metadata = {**(params.metadata or {}), "bursar_account_id": params.account_id}
        session_kwargs: dict[str, Any] = {
            "product_cart": [{"product_id": params.product_id, "quantity": quantity}],
            "return_url": params.return_url,
            "metadata": metadata,
        }
        if params.cancel_url:
            session_kwargs["cancel_url"] = params.cancel_url
        if params.customer_id:
            session_kwargs["customer"] = {"customer_id": params.customer_id}
        elif params.email:
            session_kwargs["customer"] = {"email": params.email}

        session_kwargs["extra_headers"] = _idempotency_headers(params.idempotency_key)
        session = await client.checkout_sessions.create(**session_kwargs)
        return CheckoutSessionResult(
            url=_require_provider_text(session.checkout_url, "create_checkout_session", "checkout_url"),
            provider_session_id=_require_provider_text(
                session.session_id,
                "create_checkout_session",
                "session_id",
            ),
        )

    async def create_customer_portal_session(self, params: PortalParams) -> ProviderUrlResult:
        client = self._get_client()
        session = await client.customers.customer_portal.create(
            params.customer_id,
            return_url=params.return_url,
        )
        return ProviderUrlResult(url=_require_provider_text(session.link, "create_customer_portal_session", "link"))

    async def handle_webhook(self, req: WebhookRequest) -> WebhookResult:
        client = self._get_client()
        try:
            event = client.webhooks.unwrap(
                req.raw_body,
                headers=req.headers,
                key=self._webhook_key,
            )
        except Exception as exc:
            self._logger.warning(
                "Dodo webhook verification failed",
                {"error": persisted_diagnostic_summary(exc, "webhook_verification_failed")},
            )
            return WebhookResult(
                received=False,
                retryable=False,
                provider=self.provider,
                event_id=None,
                event_type=None,
            )

        event_type = event.type
        data_dict = event.data.model_dump(mode="json")

        metadata = _normalize_metadata(data_dict.get("metadata"))

        account_id: str | None = metadata.get("bursar_account_id")

        await handle_dodo_billing_event(
            event_type=str(event_type),
            data=data_dict,
            event_timestamp=event.timestamp,
            account_id=account_id,
            metadata=metadata,
            sink=self._sink,
            logger=self._logger,
        )
        return WebhookResult(
            received=True,
            retryable=False,
            provider=self.provider,
            event_id=dodo_billing_event_id(str(event_type), data_dict, event.timestamp),
            event_type=str(event_type) or None,
        )

    async def cancel_subscription(self, subscription_id: str, idempotency_key: str) -> None:
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "cancel_at_next_billing_date": True,
            "extra_headers": _idempotency_headers(idempotency_key),
        }
        await client.subscriptions.update(
            subscription_id,
            **kwargs,
        )

    async def reactivate_subscription(self, subscription_id: str, idempotency_key: str) -> None:
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "cancel_at_next_billing_date": False,
            "extra_headers": _idempotency_headers(idempotency_key),
        }
        await client.subscriptions.update(
            subscription_id,
            **kwargs,
        )

    async def cancel_scheduled_plan_change(
        self,
        subscription_id: str,
        # Dodo cancels by subscription; the shared contract also carries Stripe's schedule ID.
        provider_operation_id: str | None = None,  # noqa: ARG002
        *,
        idempotency_key: str,
    ) -> None:
        client = self._get_client()
        kwargs: dict[str, Any] = {"extra_headers": _idempotency_headers(idempotency_key)}
        await client.subscriptions.cancel_change_plan(subscription_id, **kwargs)

    async def get_checkout_session_status(self, provider_session_id: str) -> CheckoutSessionStatus | None:
        from dodopayments import NotFoundError

        client = self._get_client()
        try:
            session = await client.checkout_sessions.retrieve(provider_session_id)
        except NotFoundError:
            return None
        return CheckoutSessionStatus(payment_status=session.payment_status)

    async def create_update_payment_method_session(self, params: UpdatePaymentMethodParams) -> ProviderUrlResult:
        client = self._get_client()
        response = await client.subscriptions.update_payment_method(
            params.subscription_id,
            payment_method={"type": "new", "return_url": params.return_url},
        )
        return ProviderUrlResult(
            url=_require_provider_text(
                response.payment_link,
                "create_update_payment_method_session",
                "payment_link",
            )
        )

    async def create_payment_method_setup_session(self, params: PaymentMethodSetupParams) -> ProviderUrlResult:
        product_id = params.product_id or self._setup_product_id
        if not product_id:
            raise ValueError("setupProductId is required for payment method setup")
        client = self._get_client()
        session = await client.checkout_sessions.create(
            product_cart=[{"product_id": product_id, "quantity": 1}],
            customer={"customer_id": params.customer_id},
            return_url=params.return_url,
            metadata={"purpose": "setup_payment_method"},
            subscription_data={"on_demand": {"mandate_only": True}},
        )
        return ProviderUrlResult(
            url=_require_provider_text(
                session.checkout_url,
                "create_payment_method_setup_session",
                "checkout_url",
            )
        )

    async def list_payment_methods(self, customer_id: str) -> list[PaymentMethodInfo]:
        client = self._get_client()
        response = await client.customers.retrieve_payment_methods(customer_id)
        result: list[PaymentMethodInfo] = []
        for payment_method in response.items:
            if payment_method.payment_method != "card":
                continue
            if not payment_method.recurring_enabled:
                continue
            if payment_method.card is None:
                raise ProviderResponseError(
                    "dodo",
                    "list_payment_methods",
                    details={"field": "card"},
                )
            card = payment_method.card
            last4 = _require_provider_text(
                card.last4_digits,
                "list_payment_methods",
                "card.last4_digits",
            )
            if len(last4) != 4 or not last4.isascii() or not last4.isdigit():
                raise ProviderResponseError(
                    "dodo",
                    "list_payment_methods",
                    details={"field": "card.last4_digits"},
                )
            result.append(
                PaymentMethodInfo(
                    id=_require_provider_text(
                        payment_method.payment_method_id,
                        "list_payment_methods",
                        "payment_method_id",
                    ),
                    last4=last4,
                    brand=_require_provider_text(
                        card.card_network,
                        "list_payment_methods",
                        "card.card_network",
                    ),
                    expiry_month=_require_provider_int(
                        card.expiry_month,
                        "list_payment_methods",
                        "card.expiry_month",
                        minimum=1,
                        maximum=12,
                    ),
                    expiry_year=_require_provider_int(
                        card.expiry_year,
                        "list_payment_methods",
                        "card.expiry_year",
                        minimum=1,
                    ),
                )
            )
        methods = deduplicate_payment_methods(result)
        if len(methods) == 1:
            methods[0] = methods[0].model_copy(update={"is_default": True})
        return methods

    async def preview_saved_payment_charge(self, params: SavedPaymentChargeParams) -> SavedPaymentChargeQuote:
        preview = await self._get_client().checkout_sessions.preview(
            product_cart=[{"product_id": params.product_id, "quantity": params.quantity}],
            customer={"customer_id": params.customer_id},
        )
        return SavedPaymentChargeQuote(
            amount_minor=_require_provider_int(
                preview.current_breakup.total_amount,
                "preview_saved_payment_charge",
                "current_breakup.total_amount",
            ),
            tax_minor=(
                _require_provider_int(
                    preview.current_breakup.tax,
                    "preview_saved_payment_charge",
                    "current_breakup.tax",
                )
                if preview.current_breakup.tax is not None
                else None
            ),
            currency=_require_provider_text(
                preview.currency,
                "preview_saved_payment_charge",
                "currency",
            ),
        )

    async def charge_saved_payment_method(self, params: SavedPaymentChargeParams) -> SavedPaymentChargeResult:
        client = self._get_client()
        metadata: dict[str, str | float | bool] = dict(params.metadata)
        session = await client.checkout_sessions.create(
            product_cart=[{"product_id": params.product_id, "quantity": params.quantity}],
            customer={"customer_id": params.customer_id},
            payment_method_id=params.payment_method_id,
            confirm=True,
            return_url=params.return_url,
            metadata=metadata,
            extra_headers=_idempotency_headers(params.idempotency_key),
        )
        payment_id = _require_provider_text(
            session.payment_id,
            "charge_saved_payment_method",
            "payment_id",
        )
        payment = await client.payments.retrieve(payment_id)
        status = _require_provider_text(
            payment.status,
            "charge_saved_payment_method",
            "status",
        )
        return SavedPaymentChargeResult.model_validate(
            {
                "provider_payment_id": _require_provider_text(
                    payment.payment_id,
                    "charge_saved_payment_method",
                    "payment_id",
                ),
                "status": status,
                "amount_minor": _require_provider_int(
                    payment.total_amount,
                    "charge_saved_payment_method",
                    "total_amount",
                ),
                "currency": _require_provider_text(
                    payment.currency,
                    "charge_saved_payment_method",
                    "currency",
                ),
            }
        )

    async def create_customer(self, params: CreateCustomerParams) -> CreateCustomerResult:
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "email": params.email,
            "name": params.name,
            "extra_headers": _idempotency_headers(params.idempotency_key),
        }
        if params.metadata:
            kwargs["metadata"] = params.metadata
        customer = await client.customers.create(**kwargs)
        return CreateCustomerResult(
            customer_id=_require_provider_text(customer.customer_id, "create_customer", "customer_id")
        )

    async def get_invoice_url(self, provider_payment_id: str) -> ProviderUrlResult | None:
        client = self._get_client()
        payment = await client.payments.retrieve(provider_payment_id)
        if payment.invoice_url:
            return ProviderUrlResult(url=_require_provider_text(payment.invoice_url, "get_invoice_url", "invoice_url"))
        return None

    async def change_plan(self, params: ChangePlanParams) -> ChangePlanResult:
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "product_id": params.product_id,
            "proration_billing_mode": params.proration_billing_mode,
            "quantity": params.quantity,
        }
        if params.effective_at:
            kwargs["effective_at"] = params.effective_at
        if params.on_payment_failure:
            kwargs["on_payment_failure"] = params.on_payment_failure
        if params.metadata:
            kwargs["metadata"] = params.metadata
        kwargs["extra_headers"] = _idempotency_headers(params.idempotency_key)
        await client.subscriptions.change_plan(params.provider_subscription_id, **kwargs)
        return ChangePlanResult()

    async def preview_change_plan(self, params: PreviewChangePlanParams) -> ChangePlanPreview:
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "product_id": params.product_id,
            "proration_billing_mode": params.proration_billing_mode,
            "quantity": params.quantity,
        }
        if params.effective_at:
            kwargs["effective_at"] = params.effective_at
        response = await client.subscriptions.preview_change_plan(params.provider_subscription_id, **kwargs)
        immediate_charge = response.immediate_charge
        summary = immediate_charge.summary
        line_items: list[ChangePlanLineItem] = []
        for item in immediate_charge.line_items:
            if item.type == "subscription":
                operation = "preview_change_plan"
                product_id = _require_provider_text(
                    item.product_id,
                    operation,
                    "immediate_charge.line_items.product_id",
                )
                name = item.name or item.description
                name = _require_provider_text(name, operation, "immediate_charge.line_items.name")
                unit_price = _require_provider_int(
                    item.unit_price,
                    operation,
                    "immediate_charge.line_items.unit_price",
                )
                quantity = _require_provider_int(
                    item.quantity,
                    operation,
                    "immediate_charge.line_items.quantity",
                    minimum=1,
                )
                proration_factor = _require_provider_number(
                    item.proration_factor,
                    operation,
                    "immediate_charge.line_items.proration_factor",
                )
                currency = _require_provider_text(
                    item.currency,
                    operation,
                    "immediate_charge.line_items.currency",
                )
                tax = (
                    _require_provider_int(
                        item.tax,
                        operation,
                        "immediate_charge.line_items.tax",
                    )
                    if item.tax is not None
                    else 0
                )
                line_items.append(
                    ChangePlanLineItem(
                        product_id=product_id,
                        name=name,
                        unit_price=unit_price,
                        quantity=quantity,
                        proration_factor=proration_factor,
                        currency=currency,
                        tax=tax,
                        subtotal=int(
                            (Decimal(unit_price * quantity) * Decimal(str(proration_factor))).quantize(
                                Decimal("1"), rounding=ROUND_HALF_UP
                            )
                        ),
                    )
                )
        new_plan = response.new_plan
        return ChangePlanPreview(
            total_amount=_require_provider_int(
                summary.total_amount,
                "preview_change_plan",
                "immediate_charge.summary.total_amount",
            ),
            settlement_amount=_require_provider_int(
                summary.settlement_amount,
                "preview_change_plan",
                "immediate_charge.summary.settlement_amount",
            ),
            currency=_require_provider_text(
                summary.settlement_currency,
                "preview_change_plan",
                "immediate_charge.summary.settlement_currency",
            ),
            line_items=line_items,
            effective_at=_require_provider_instant(
                immediate_charge.effective_at,
                "preview_change_plan",
                "immediate_charge.effective_at",
            ),
            recurring_amount=(
                _require_provider_int(
                    new_plan.recurring_pre_tax_amount,
                    "preview_change_plan",
                    "new_plan.recurring_pre_tax_amount",
                )
                if new_plan is not None and new_plan.recurring_pre_tax_amount is not None
                else None
            ),
            recurring_currency=(
                _require_provider_text(
                    new_plan.currency,
                    "preview_change_plan",
                    "new_plan.currency",
                )
                if new_plan is not None and new_plan.currency is not None
                else None
            ),
            next_billing_date=(
                _require_provider_instant(
                    new_plan.next_billing_date,
                    "preview_change_plan",
                    "new_plan.next_billing_date",
                )
                if new_plan is not None and new_plan.next_billing_date is not None
                else None
            ),
            tax_amount=(
                _require_provider_int(
                    summary.settlement_tax,
                    "preview_change_plan",
                    "immediate_charge.summary.settlement_tax",
                )
                if summary.settlement_tax is not None
                else _require_provider_int(
                    summary.tax,
                    "preview_change_plan",
                    "immediate_charge.summary.tax",
                )
                if summary.tax is not None
                else None
            ),
            customer_credits=_require_provider_int(
                summary.customer_credits,
                "preview_change_plan",
                "immediate_charge.summary.customer_credits",
            ),
        )
