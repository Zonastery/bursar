package bursar

import "sort"

// PublicCatalogWindow is the provider-safe window description exposed to
// product surfaces. Rolling and plan-assignment windows are normalized to the
// same unit/count shape as calendar windows.
type PublicCatalogWindow struct {
	Type     string  `json:"type"`
	Unit     string  `json:"unit"`
	Count    int     `json:"count"`
	Timezone *string `json:"timezone,omitempty"`
}

type PublicCatalogOffer struct {
	Key             string           `json:"key"`
	Type            string           `json:"type"`
	DisplayName     string           `json:"displayName"`
	Description     *string          `json:"description,omitempty"`
	SortOrder       int              `json:"sortOrder"`
	Price           OfferPrice       `json:"price"`
	BillingInterval *BillingInterval `json:"billingInterval,omitempty"`
	CreditsPerUnit  *string          `json:"creditsPerUnit,omitempty"`
	Quantity        *OfferQuantity   `json:"quantity,omitempty"`
}

type PublicCatalogQuota struct {
	Operation   string              `json:"operation"`
	Measure     string              `json:"measure"`
	Limit       string              `json:"limit"`
	Window      PublicCatalogWindow `json:"window"`
	Enforcement string              `json:"enforcement"`
}

type PublicCatalogAllowance struct {
	Amount   string              `json:"amount"`
	Priority int                 `json:"priority"`
	Window   PublicCatalogWindow `json:"window"`
}

type PublicCatalogPlan struct {
	Key         string                        `json:"key"`
	DisplayName string                        `json:"displayName"`
	Description *string                       `json:"description,omitempty"`
	Rank        int                           `json:"rank"`
	Features    map[string]any                `json:"features"`
	Allowance   *PublicCatalogAllowance       `json:"allowance,omitempty"`
	Quotas      map[string]PublicCatalogQuota `json:"quotas"`
	Offers      []PublicCatalogOffer          `json:"offers"`
}

// PublicCatalog deliberately excludes provider references, adapter names,
// fulfillment behavior, and other server-only commerce configuration.
type PublicCatalog struct {
	Version       int                  `json:"version"`
	DefaultPlan   *string              `json:"defaultPlan"`
	CreditDisplay *CreditDisplay       `json:"creditDisplay"`
	Plans         []PublicCatalogPlan  `json:"plans"`
	Topups        []PublicCatalogOffer `json:"topups"`
}

// ProjectPublicCatalog creates a provider-secret-free catalog suitable for a
// browser, mobile client, or product API response. The returned values are
// detached from the source configuration.
func ProjectPublicCatalog(config *BursarConfig) PublicCatalog {
	result := PublicCatalog{Version: 1, Plans: []PublicCatalogPlan{}, Topups: []PublicCatalogOffer{}}
	if config == nil {
		return result
	}
	result.Version = config.Version
	result.DefaultPlan = cloneStringPointer(config.Catalog.DefaultPlan)
	if config.Credits.Display != nil {
		display := *config.Credits.Display
		result.CreditDisplay = &display
	}
	for key, plan := range config.Plans {
		projected := PublicCatalogPlan{
			Key: key, DisplayName: plan.DisplayName, Description: cloneStringPointer(plan.Description), Rank: plan.Rank,
			Features: cloneAnyMap(plan.Features), Quotas: make(map[string]PublicCatalogQuota, len(plan.Quotas)), Offers: []PublicCatalogOffer{},
		}
		if plan.CreditAllowance != nil {
			projected.Allowance = &PublicCatalogAllowance{
				Amount: amountConfigString(plan.CreditAllowance.Amount), Priority: plan.CreditAllowance.Priority,
				Window: projectPublicWindow(plan.CreditAllowance.Window),
			}
		}
		for quotaKey, quota := range plan.Quotas {
			projected.Quotas[quotaKey] = PublicCatalogQuota{
				Operation: quota.Operation, Measure: quota.Measure, Limit: amountConfigString(quota.Limit),
				Window: projectPublicWindow(quota.Window), Enforcement: quota.Enforcement,
			}
		}
		for offerKey, offer := range config.Commerce.Offers {
			if offer.Type == "subscription" && offer.Plan != nil && *offer.Plan == key {
				projected.Offers = append(projected.Offers, projectPublicOffer(offerKey, offer))
			}
		}
		sortPublicOffers(projected.Offers)
		result.Plans = append(result.Plans, projected)
	}
	for key, offer := range config.Commerce.Offers {
		if offer.Type == "topup" {
			result.Topups = append(result.Topups, projectPublicOffer(key, offer))
		}
	}
	sort.Slice(result.Plans, func(left, right int) bool {
		if result.Plans[left].Rank != result.Plans[right].Rank {
			return result.Plans[left].Rank < result.Plans[right].Rank
		}
		return result.Plans[left].Key < result.Plans[right].Key
	})
	sortPublicOffers(result.Topups)
	return result
}

func projectPublicWindow(window Window) PublicCatalogWindow {
	result := PublicCatalogWindow{Type: window.Type}
	switch window.Type {
	case "calendar":
		result.Unit, result.Count = window.Unit, window.Count
		if window.Timezone != "" {
			timezone := window.Timezone
			result.Timezone = &timezone
		}
	case "plan_assignment":
		if window.Interval != nil {
			result.Unit, result.Count = window.Interval.Unit, window.Interval.Count
		}
		if window.Timezone != "" {
			timezone := window.Timezone
			result.Timezone = &timezone
		}
	case "rolling":
		if window.Duration != nil {
			result.Unit, result.Count = window.Duration.Unit, window.Duration.Count
		}
	}
	return result
}

func projectPublicOffer(key string, offer CommerceOffer) PublicCatalogOffer {
	projected := PublicCatalogOffer{
		Key: key, Type: offer.Type, DisplayName: offer.DisplayName, Description: cloneStringPointer(offer.Description),
		SortOrder: offer.SortOrder, Price: offer.Price,
	}
	if offer.Type == "subscription" && offer.BillingInterval != nil {
		interval := *offer.BillingInterval
		projected.BillingInterval = &interval
	}
	if offer.Type == "topup" {
		if offer.CreditsPerUnit != nil {
			credits := amountConfigString(*offer.CreditsPerUnit)
			projected.CreditsPerUnit = &credits
		}
		if offer.Quantity != nil {
			quantity := *offer.Quantity
			projected.Quantity = &quantity
		}
	}
	return projected
}

func sortPublicOffers(offers []PublicCatalogOffer) {
	sort.Slice(offers, func(left, right int) bool {
		if offers[left].SortOrder != offers[right].SortOrder {
			return offers[left].SortOrder < offers[right].SortOrder
		}
		return offers[left].Key < offers[right].Key
	})
}

func cloneStringPointer(value *string) *string {
	if value == nil {
		return nil
	}
	cloned := *value
	return &cloned
}
