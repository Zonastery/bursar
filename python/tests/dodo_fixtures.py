"""Realistic Dodo webhook payloads.

These match what Dodo actually sends — no ``data.id``/``data.payment_id`` on
subscription events, and dates in JS ``Date.prototype.toString()`` format.
"""

DODO_JS_DATE = "Sat Jul 18 2026 05:15:24 GMT+0000 (Coordinated Universal Time)"
DODO_ISO_DATE = "2026-07-18T05:15:24+00:00"

DODO_SUBSCRIPTION_ACTIVE = {
    "subscription_id": "sub_dodo_active_001",
    "status": "active",
    "product_id": "prod_monk",
    "payment_frequency_interval": "Month",
    "payment_frequency_count": 1,
    "previous_billing_date": DODO_JS_DATE,
    "next_billing_date": "Sat Aug 18 2026 05:15:24 GMT+0000 (Coordinated Universal Time)",
}

DODO_SUBSCRIPTION_ACTIVE_PLAN_SLUG = {
    "subscription_id": "sub_dodo_slug_001",
    "status": "active",
    "payment_frequency_interval": "Month",
    "payment_frequency_count": 1,
    "previous_billing_date": DODO_JS_DATE,
    "next_billing_date": DODO_JS_DATE,
}

DODO_SUBSCRIPTION_ACTIVE_NO_DATES = {
    "subscription_id": "sub_dodo_no_dates",
    "status": "active",
    "product_id": "prod_monk",
}

DODO_SUBSCRIPTION_RENEWED = {
    "subscription_id": "sub_dodo_renewed_001",
    "status": "active",
    "product_id": "prod_monk",
    "payment_frequency_interval": "Month",
    "payment_frequency_count": 1,
    "previous_billing_date": DODO_JS_DATE,
    "next_billing_date": DODO_JS_DATE,
}

DODO_SUBSCRIPTION_UPDATED = {
    "subscription_id": "sub_dodo_updated_001",
    "status": "active",
    "product_id": "prod_monk",
    "next_billing_date": DODO_JS_DATE,
}

DODO_SUBSCRIPTION_CANCELLED = {
    "subscription_id": "sub_dodo_cancelled_001",
}

DODO_SUBSCRIPTION_EXPIRED = {
    "subscription_id": "sub_dodo_expired_001",
}

DODO_SUBSCRIPTION_FAILED = {
    "subscription_id": "sub_dodo_failed_001",
}

DODO_SUBSCRIPTION_ON_HOLD = {
    "subscription_id": "sub_dodo_on_hold_001",
}

DODO_SUBSCRIPTION_CANCELLATION_SCHEDULED = {
    "subscription_id": "sub_dodo_cancel_sched_001",
}

DODO_SUBSCRIPTION_CANCELLATION_UNSCHEDULED = {
    "subscription_id": "sub_dodo_cancel_unsched_001",
}

DODO_SUBSCRIPTION_PLAN_CHANGED = {
    "subscription_id": "sub_dodo_plan_change_001",
    "product_id": "prod_sage",
}

DODO_PAYMENT_SUCCEEDED = {
    "id": "pay_dodo_success_001",
    "payment_id": "pay_dodo_success_001",
    "subscription_id": "sub_dodo_active_001",
    "settlement_amount": 2999,
    "settlement_currency": "USD",
    "settlement_tax": 240,
    "product_id": "prod_monk",
}

DODO_PAYMENT_FAILED = {
    "id": "pay_dodo_failed_001",
    "payment_id": "pay_dodo_failed_001",
    "subscription_id": "sub_dodo_active_001",
}

DODO_CHECKOUT_EXPIRED = {
    "id": "checkout_dodo_expired_001",
}

DODO_REFUND_SUCCEEDED = {
    "id": "refund_dodo_001",
    "payment_id": "pay_dodo_success_001",
    "refund_amount": 2999,
    "currency": "USD",
    "reason": "Customer requested",
}

DODO_DISPUTE_CREATED = {
    "id": "dispute_dodo_001",
    "payment_id": "pay_dodo_success_001",
    "reason": "fraudulent",
}

DODO_DISPUTE_WON_CLOSED = {
    "id": "dispute_dodo_won_001",
    "payment_id": "pay_dodo_success_001",
}
