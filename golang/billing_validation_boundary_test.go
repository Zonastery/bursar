package bursar

import (
	"testing"
	"time"
)

func TestBillingEventValidationRejectsMalformedFinancialLifecycleData(t *testing.T) {
	now := time.Date(2026, 8, 19, 4, 5, 6, 0, time.UTC)
	zero := time.Time{}
	base := func(eventType BillingEventType) BillingEvent {
		return BillingEvent{EventID: "evt_1", Provider: "stripe", Type: eventType, OccurredAt: now}
	}
	validSubscription := func() *BillingSubscription {
		return &BillingSubscription{ProviderSubscriptionID: "sub_1", Status: "active", Interval: "month", IntervalCount: 1}
	}
	validInvoice := func() *BillingInvoice {
		return &BillingInvoice{ProviderInvoiceID: "in_1", Status: "paid", Currency: "USD"}
	}
	validPayment := func() *BillingPayment {
		return &BillingPayment{ProviderPaymentID: "pay_1", AmountMinor: 1_299, TaxMinor: 99, Currency: "USD", Purpose: "subscription", Status: "succeeded"}
	}
	validRefund := func() *BillingRefund {
		return &BillingRefund{ProviderRefundID: "re_1", ProviderPaymentID: "pay_1", AmountMinor: 500, Currency: "USD", Status: "succeeded"}
	}
	validDispute := func() *BillingDispute {
		return &BillingDispute{ProviderDisputeID: "dp_1", ProviderPaymentID: "pay_1", Status: "needs_response"}
	}

	tests := map[string]BillingEvent{
		"missing type": func() BillingEvent {
			event := base("")
			return event
		}(),
		"missing customer":     base(BillingEventCustomerCreated),
		"missing subscription": base(BillingEventSubscriptionCreated),
		"missing invoice":      base(BillingEventInvoiceCreated),
		"missing payment":      base(BillingEventPaymentSucceeded),
		"missing refund":       base(BillingEventRefundCreated),
		"missing dispute":      base(BillingEventDisputeCreated),
		"empty customer": func() BillingEvent {
			event := base(BillingEventCustomerCreated)
			event.Customer = &BillingCustomer{}
			return event
		}(),
		"subscription id": func() BillingEvent {
			event := base(BillingEventSubscriptionCreated)
			event.Subscription = &BillingSubscription{Status: "active"}
			return event
		}(),
		"subscription status": func() BillingEvent {
			event := base(BillingEventSubscriptionUpdated)
			event.Subscription = validSubscription()
			event.Subscription.Status = "unknown"
			return event
		}(),
		"subscription interval": func() BillingEvent {
			event := base(BillingEventSubscriptionUpdated)
			event.Subscription = validSubscription()
			event.Subscription.Interval = "fortnight"
			return event
		}(),
		"subscription interval count": func() BillingEvent {
			event := base(BillingEventSubscriptionUpdated)
			event.Subscription = validSubscription()
			event.Subscription.IntervalCount = 0
			return event
		}(),
		"subscription reference": func() BillingEvent {
			event := base(BillingEventSubscriptionUpdated)
			event.Subscription = validSubscription()
			event.Subscription.Refs = &ProviderRef{}
			return event
		}(),
		"subscription instant": func() BillingEvent {
			event := base(BillingEventSubscriptionUpdated)
			event.Subscription = validSubscription()
			event.Subscription.PeriodStart = &zero
			return event
		}(),
		"invoice id": func() BillingEvent {
			event := base(BillingEventInvoiceCreated)
			event.Invoice = &BillingInvoice{Status: "paid", Currency: "USD"}
			return event
		}(),
		"invoice status": func() BillingEvent {
			event := base(BillingEventInvoiceUpdated)
			event.Invoice = validInvoice()
			event.Invoice.Status = "unknown"
			return event
		}(),
		"invoice amount": func() BillingEvent {
			event := base(BillingEventInvoiceUpdated)
			event.Invoice = validInvoice()
			event.Invoice.AmountDueMinor = -1
			return event
		}(),
		"invoice currency": func() BillingEvent {
			event := base(BillingEventInvoiceUpdated)
			event.Invoice = validInvoice()
			event.Invoice.Currency = "usd"
			return event
		}(),
		"invoice instant": func() BillingEvent {
			event := base(BillingEventInvoiceUpdated)
			event.Invoice = validInvoice()
			event.Invoice.PeriodEnd = &zero
			return event
		}(),
		"payment id": func() BillingEvent {
			event := base(BillingEventPaymentSucceeded)
			event.Payment = validPayment()
			event.Payment.ProviderPaymentID = ""
			return event
		}(),
		"payment amount": func() BillingEvent {
			event := base(BillingEventPaymentSucceeded)
			event.Payment = validPayment()
			event.Payment.TaxMinor = -1
			return event
		}(),
		"payment currency": func() BillingEvent {
			event := base(BillingEventPaymentSucceeded)
			event.Payment = validPayment()
			event.Payment.Currency = "US"
			return event
		}(),
		"payment purpose": func() BillingEvent {
			event := base(BillingEventPaymentSucceeded)
			event.Payment = validPayment()
			event.Payment.Purpose = "donation"
			return event
		}(),
		"payment status": func() BillingEvent {
			event := base(BillingEventPaymentSucceeded)
			event.Payment = validPayment()
			event.Payment.Status = "unknown"
			return event
		}(),
		"payment reference": func() BillingEvent {
			event := base(BillingEventPaymentSucceeded)
			event.Payment = validPayment()
			event.Payment.Refs = &ProviderRef{}
			return event
		}(),
		"refund ids": func() BillingEvent {
			event := base(BillingEventRefundCreated)
			event.Refund = validRefund()
			event.Refund.ProviderPaymentID = ""
			return event
		}(),
		"refund amount": func() BillingEvent {
			event := base(BillingEventRefundCreated)
			event.Refund = validRefund()
			event.Refund.AmountMinor = 0
			return event
		}(),
		"refund currency": func() BillingEvent {
			event := base(BillingEventRefundCreated)
			event.Refund = validRefund()
			event.Refund.Currency = "usd"
			return event
		}(),
		"refund status": func() BillingEvent {
			event := base(BillingEventRefundUpdated)
			event.Refund = validRefund()
			event.Refund.Status = "unknown"
			return event
		}(),
		"dispute ids": func() BillingEvent {
			event := base(BillingEventDisputeCreated)
			event.Dispute = validDispute()
			event.Dispute.ProviderDisputeID = ""
			return event
		}(),
		"dispute status": func() BillingEvent {
			event := base(BillingEventDisputeUpdated)
			event.Dispute = validDispute()
			event.Dispute.Status = "unknown"
			return event
		}(),
	}
	for name, event := range tests {
		t.Run(name, func(t *testing.T) {
			if err := event.Validate(); err == nil {
				t.Fatal("malformed billing event accepted")
			}
		})
	}
}

func TestBillingEventValidationAcceptsExactMinorUnits(t *testing.T) {
	event := BillingEvent{
		EventID: "evt_exact", Provider: "stripe", Type: BillingEventPaymentSucceeded,
		OccurredAt: time.Now().UTC(),
		Payment: &BillingPayment{
			ProviderPaymentID: "pay_exact", AmountMinor: 9_007_199_254_740_991,
			TaxMinor: 123, Currency: "USD", Purpose: "credit_topup", Status: "succeeded",
		},
	}
	if err := event.Validate(); err != nil {
		t.Fatalf("exact integer-valued payment rejected: %v", err)
	}
}
