// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

// ProviderReceipt is the framework-neutral accounting projection from one
// completed AI provider call. Operational telemetry remains with the host;
// only exact pricing inputs and financial correlation metadata belong here.
type ProviderReceipt struct {
	Metrics  UsageMetrics   `json:"metrics"`
	Metadata CreditMetadata `json:"metadata,omitempty"`
}

// Validate rejects receipts that cannot be priced deterministically.
func (r ProviderReceipt) Validate() error {
	return r.Metrics.Validate()
}

// Clone returns a receipt with detached top-level maps, suitable for retaining
// beyond a provider callback. Values stored inside dimensions or metadata must
// themselves be immutable while a settlement is pending.
func (r ProviderReceipt) Clone() ProviderReceipt {
	measures := make(map[string]Amount, len(r.Metrics.Measures))
	for key, value := range r.Metrics.Measures {
		measures[key] = value
	}
	return ProviderReceipt{
		Metrics: UsageMetrics{
			Operation:  r.Metrics.Operation,
			Measures:   measures,
			Dimensions: cloneAnyMap(r.Metrics.Dimensions),
			Metadata:   cloneAnyMap(r.Metrics.Metadata),
		},
		Metadata: r.Metadata.Clone(),
	}
}

// ProviderReceiptSource bridges request-local provider responses to billing
// integrations without coupling Bursar to one model SDK. Begin starts capture;
// Finish ends it and returns nil when no authoritative provider call completed.
type ProviderReceiptSource interface {
	Begin()
	Finish() *ProviderReceipt
}
