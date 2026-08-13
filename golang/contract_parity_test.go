package bursar

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

type facadeContractEntry struct {
	JavaScript string `json:"javascript"`
	Python     string `json:"python"`
}

func TestSharedFacadeContract(t *testing.T) {
	t.Parallel()

	data, err := os.ReadFile(filepath.Join("..", "common", "facade-contract.json"))
	if err != nil {
		t.Fatalf("read shared facade contract: %v", err)
	}
	var contract map[string][]facadeContractEntry
	if err := json.Unmarshal(data, &contract); err != nil {
		t.Fatalf("decode shared facade contract: %v", err)
	}

	surfaces := map[string]reflect.Type{
		"bursar":   reflect.TypeOf((*Bursar)(nil)),
		"catalog":  reflect.TypeOf((*CatalogService)(nil)),
		"accounts": reflect.TypeOf((*AccountService)(nil)),
		"credits":  reflect.TypeOf((*CreditsService)(nil)),
		"runtime":  reflect.TypeOf((*BursarRuntime)(nil)),
	}
	goMethods := map[string]map[string]string{
		"bursar": {
			"loadCatalog":        "LoadCatalog",
			"requireBilling":     "RequireBilling",
			"requireCommerce":    "RequireCommerce",
			"ingestBillingEvent": "IngestBillingEvent",
		},
		"catalog": {
			"getActive":          "GetActive",
			"isLoaded":           "IsLoaded",
			"load":               "Load",
			"refresh":            "Refresh",
			"invalidate":         "Invalidate",
			"getConfig":          "GetConfig",
			"publicView":         "PublicView",
			"publishDraft":       "PublishDraft",
			"activate":           "Activate",
			"publishAndActivate": "PublishAndActivate",
			"setRevisionPin":     "SetRevisionPin",
			"applyDueChanges":    "ApplyDueChanges",
		},
		"accounts": {
			"onAccountCreated": "OnAccountCreated",
		},
		"credits": {
			"addCredits":           "AddCredits",
			"deductCredits":        "DeductCredits",
			"deduct":               "Deduct",
			"recordUsage":          "RecordUsage",
			"reserve":              "Reserve",
			"settle":               "Settle",
			"release":              "Release",
			"renew":                "Renew",
			"runBilled":            "RunBilled",
			"beginBilledOperation": "BeginBilledOperation",
			"deductFlatJob":        "DeductFlatJob",
			"refundCredits":        "RefundCredits",
		},
		"runtime": {
			"start":  "Start",
			"health": "Health",
			"flush":  "Flush",
			"close":  "Close",
		},
	}

	if len(contract) != len(surfaces) {
		t.Fatalf("shared contract has %d surfaces, Go maps %d", len(contract), len(surfaces))
	}
	for surface, entries := range contract {
		typeOf, ok := surfaces[surface]
		if !ok {
			t.Fatalf("shared contract added unmapped surface %q", surface)
		}
		mapped := goMethods[surface]
		if len(entries) != len(mapped) {
			t.Fatalf("shared %s contract has %d operations, Go maps %d", surface, len(entries), len(mapped))
		}
		for _, entry := range entries {
			goName, ok := mapped[entry.JavaScript]
			if !ok {
				t.Errorf("Go %s surface has no deliberate mapping for shared operation %q (%s)", surface, entry.JavaScript, entry.Python)
				continue
			}
			if _, ok := typeOf.MethodByName(goName); !ok {
				t.Errorf("Go %s surface is missing %s for shared operation %q", surface, goName, entry.JavaScript)
			}
		}
	}
}

type commerceParityFixture struct {
	Catalog     map[string]any `json:"catalog"`
	Transitions []struct {
		CurrentPlan     string  `json:"current_plan"`
		CurrentInterval string  `json:"current_interval"`
		TargetOffer     string  `json:"target_offer"`
		Classification  string  `json:"classification"`
		Effective       *string `json:"effective"`
		Proration       *string `json:"proration"`
	} `json:"transitions"`
	ErrorCodes     map[string]string `json:"error_codes"`
	PublicContract map[string]string `json:"public_contract"`
}

func TestSharedCommerceParityFixture(t *testing.T) {
	t.Parallel()

	data, err := os.ReadFile(filepath.Join("..", "common", "commerce-parity.json"))
	if err != nil {
		t.Fatalf("read shared commerce fixture: %v", err)
	}
	var fixture commerceParityFixture
	if err := json.Unmarshal(data, &fixture); err != nil {
		t.Fatalf("decode shared commerce fixture: %v", err)
	}
	config, err := LoadConfigFromMap(fixture.Catalog)
	if err != nil {
		t.Fatalf("load shared commerce catalog: %v", err)
	}

	for _, transition := range fixture.Transitions {
		transition := transition
		t.Run(transition.Classification, func(t *testing.T) {
			offer, ok := config.Commerce.Offers[transition.TargetOffer]
			if !ok || offer.Plan == nil || offer.BillingInterval == nil {
				t.Fatalf("fixture offer %q is not a subscription offer", transition.TargetOffer)
			}
			classification, err := classifyPlanChange(
				config,
				transition.CurrentPlan,
				transition.CurrentInterval,
				*offer.Plan,
				offer.BillingInterval.Unit,
			)
			if err != nil {
				t.Fatalf("classify shared transition: %v", err)
			}
			if got := string(classification); got != transition.Classification {
				t.Fatalf("classification = %q, want %q", got, transition.Classification)
			}
			var effective, proration *string
			if classification != PlanChangeUnchanged {
				policy, ok := config.Commerce.SubscriptionChanges[string(classification)]
				if !ok {
					t.Fatalf("missing policy for %q", classification)
				}
				effective, proration = &policy.Effective, &policy.Proration
			}
			if !reflect.DeepEqual(effective, transition.Effective) {
				t.Errorf("effective = %v, want %v", effective, transition.Effective)
			}
			if !reflect.DeepEqual(proration, transition.Proration) {
				t.Errorf("proration = %v, want %v", proration, transition.Proration)
			}
		})
	}

	wantCodes := map[string]ErrorCode{
		"unknown_offer":       ErrorCodeUnknownOffer,
		"invalid_quantity":    ErrorCodeInvalidOfferQuantity,
		"quote_changed":       ErrorCodeQuoteChanged,
		"provider_capability": ErrorCodeProviderCapabilityNotSupported,
	}
	for key, code := range wantCodes {
		if got := fixture.ErrorCodes[key]; got != string(code) {
			t.Errorf("shared error code %s = %q, Go = %q", key, got, code)
		}
	}

	wantPublicContract := map[string]string{
		"offer_input":          "offerKey",
		"quote_field":          "quoteFingerprint",
		"provider_product_ids": "provider_internal",
	}
	if !reflect.DeepEqual(fixture.PublicContract, wantPublicContract) {
		t.Fatalf("public contract = %#v, want %#v", fixture.PublicContract, wantPublicContract)
	}
	assertStructHasField(t, reflect.TypeOf(CreateCheckoutInput{}), "OfferKey")
	assertStructHasField(t, reflect.TypeOf(PreviewPlanChangeInput{}), "OfferKey")
	assertStructHasField(t, reflect.TypeOf(ConfirmPlanChangeInput{}), "QuoteFingerprint")
	for _, typeOf := range []reflect.Type{reflect.TypeOf(CreateCheckoutInput{}), reflect.TypeOf(PreviewPlanChangeInput{}), reflect.TypeOf(ConfirmPlanChangeInput{})} {
		for _, forbidden := range []string{"ProductID", "PriceID", "QuoteHash"} {
			if _, ok := typeOf.FieldByName(forbidden); ok {
				t.Errorf("public %s unexpectedly exposes provider/internal field %s", typeOf.Name(), forbidden)
			}
		}
	}
}

func assertStructHasField(t *testing.T, typeOf reflect.Type, field string) {
	t.Helper()
	if _, ok := typeOf.FieldByName(field); !ok {
		t.Errorf("public %s is missing field %s", typeOf.Name(), field)
	}
}
