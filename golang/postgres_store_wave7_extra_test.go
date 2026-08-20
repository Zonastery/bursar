// Copyright 2026 Zonastery
// SPDX-License-Identifier: AGPL-3.0-only

package bursar

import (
	"testing"

	"github.com/google/uuid"
)

func TestPostgresStoreTeamLifecycleAndTenantBoundary(t *testing.T) {
	ctx, cancel, config := financialContext(t)
	defer cancel()
	store := openPostgresIntegrationStore(t, ctx, config, config.tenantID)
	defer store.Close()
	if databaseURL, err := store.DatabaseURL(); err != nil || databaseURL != config.databaseURL {
		t.Fatalf("owned store database URL = %q, error = %v", databaseURL, err)
	}
	secondary := openPostgresIntegrationStore(t, ctx, config, config.secondaryTenantID)
	defer secondary.Close()

	ownerID := uuid.NewString()
	memberID := uuid.NewString()
	team, err := store.CreateTeam(ctx, ownerID, "coverage team", CreateTeamOptions{
		IdempotencyKey: "store-wave7-team-" + ownerID,
		InitialBalance: MustAmount("10.125000"),
	})
	if err != nil || team.TeamID == "" || team.Name != "coverage team" || team.Idempotent {
		t.Fatalf("create team = %+v, error = %v", team, err)
	}
	replay, err := store.CreateTeam(ctx, ownerID, "coverage team", CreateTeamOptions{
		IdempotencyKey: "store-wave7-team-" + ownerID,
		InitialBalance: MustAmount("10.125000"),
	})
	if err != nil || replay.TeamID != team.TeamID || !replay.Idempotent {
		t.Fatalf("create team replay = %+v, first = %+v, error = %v", replay, team, err)
	}

	balance, err := store.GetTeamBalance(ctx, team.TeamID)
	if err != nil || balance == nil || !balance.Balance.Equal(MustAmount("10.125000")) || balance.MemberCount != 1 {
		t.Fatalf("team balance = %+v, error = %v", balance, err)
	}
	if isolated, err := secondary.GetTeamBalance(ctx, team.TeamID); err != nil || isolated != nil {
		t.Fatalf("cross-tenant team balance = %+v, error = %v", isolated, err)
	}

	member, err := store.AddTeamMember(ctx, team.TeamID, memberID, AddTeamMemberOptions{Role: TeamRoleMember, SpendCap: amountPointer(MustAmount("4"))})
	if err != nil || member.TeamID != team.TeamID || member.UserID != memberID || member.Role != TeamRoleMember {
		t.Fatalf("add team member = %+v, error = %v", member, err)
	}
	members, err := store.GetTeamMembers(ctx, team.TeamID)
	if err != nil || len(members) != 2 {
		t.Fatalf("team members = %+v, error = %v", members, err)
	}
	removed, err := store.RemoveTeamMember(ctx, team.TeamID, memberID)
	if err != nil || !removed {
		t.Fatalf("remove team member = %v, error = %v", removed, err)
	}
	removed, err = store.RemoveTeamMember(ctx, team.TeamID, memberID)
	if err != nil || removed {
		t.Fatalf("remove absent team member = %v, error = %v", removed, err)
	}
	removed, err = store.RemoveTeamMember(ctx, team.TeamID, ownerID)
	if err != nil || removed {
		t.Fatalf("remove final owner = %v, error = %v", removed, err)
	}
	if removed, err := secondary.RemoveTeamMember(ctx, team.TeamID, memberID); err != nil || removed {
		t.Fatalf("cross-tenant member removal = %v, error = %v", removed, err)
	}

	if _, err := store.AddTeamMember(ctx, team.TeamID, memberID, AddTeamMemberOptions{}); err != nil {
		t.Fatalf("re-add team member: %v", err)
	}
	deduction, err := store.DeductTeam(ctx, team.TeamID, memberID, MustAmount("1.125000"), TeamDeductionOptions{
		Operation:      "completion",
		IdempotencyKey: "store-wave7-team-deduct-" + team.TeamID,
	})
	if err != nil || deduction.EntryID == "" || !deduction.Amount.Equal(MustAmount("1.125000")) || deduction.Idempotent {
		t.Fatalf("team deduction = %+v, error = %v", deduction, err)
	}
	deductionReplay, err := store.DeductTeam(ctx, team.TeamID, memberID, MustAmount("1.125000"), TeamDeductionOptions{
		Operation:      "completion",
		IdempotencyKey: "store-wave7-team-deduct-" + team.TeamID,
	})
	if err != nil || deductionReplay.EntryID != deduction.EntryID || !deductionReplay.Idempotent {
		t.Fatalf("team deduction replay = %+v, first = %+v, error = %v", deductionReplay, deduction, err)
	}
	if _, err := store.DeductTeam(ctx, team.TeamID, memberID, MustAmount("20"), TeamDeductionOptions{Operation: "completion", IdempotencyKey: "store-wave7-team-overdraft-" + team.TeamID}); err != nil {
		t.Fatalf("team overdraft RPC returned transport error: %v", err)
	}
}

func TestPostgresStoreGrantProgramReplayAndDomainOutcomes(t *testing.T) {
	ctx, cancel, config := financialContext(t)
	defer cancel()
	sdk, store := newFinancialSDK(t, ctx, config, &financialProvider{name: "alpha"})
	catalog := financialCatalogConfig(t)
	credits := catalog["credits"].(map[string]any)
	credits["grant_programs"] = map[string]any{
		"coverage_award": map[string]any{
			"trigger": "manual",
			"awards":  []any{map[string]any{"amount": "3.125000", "bucket": "general"}},
		},
	}
	if _, err := sdk.Catalog.PublishAndActivate(ctx, catalog, "store-wave7-grant-program", newAssignmentsRollout(catalog)); err != nil {
		t.Fatalf("publish grant program catalog: %v", err)
	}

	subjectID := uuid.NewString()
	request := ExecuteGrantProgramRequest{
		Trigger: "manual", ProgramKey: "coverage_award", SubjectID: subjectID,
		EventKey: "store-wave7-grant-event-" + subjectID,
		Metadata: map[string]any{"source": "integration"},
	}
	awards, err := store.ExecuteGrantProgram(ctx, request)
	if err != nil || len(awards) != 1 || awards[0].ErrorCode != "" || awards[0].Idempotent || !awards[0].Amount.Equal(MustAmount("3.125000")) || awards[0].LedgerEntryID == "" {
		t.Fatalf("grant awards = %+v, error = %v", awards, err)
	}
	balance, err := store.GetBalance(ctx, subjectID)
	if err != nil || !balance.Balance.Equal(MustAmount("3.125000")) {
		t.Fatalf("grant balance = %+v, error = %v", balance, err)
	}
	available, err := store.GetAvailable(ctx, subjectID)
	if err != nil || !available.Available.Equal(MustAmount("3.125000")) || !available.Reserved.Equal(DecimalZero) {
		t.Fatalf("grant available = %+v, error = %v", available, err)
	}
	buckets, err := store.GetBucketBalances(ctx, subjectID)
	if err != nil || !buckets.TotalBalance.Equal(MustAmount("3.125000")) || len(buckets.Buckets) == 0 {
		t.Fatalf("grant bucket balances = %+v, error = %v", buckets, err)
	}
	replay, err := store.ExecuteGrantProgram(ctx, request)
	if err != nil || len(replay) != 1 || !replay[0].Idempotent || replay[0].LedgerEntryID != awards[0].LedgerEntryID {
		t.Fatalf("grant replay = %+v, first = %+v, error = %v", replay, awards, err)
	}
	unknown, err := store.ExecuteGrantProgram(ctx, ExecuteGrantProgramRequest{Trigger: "manual", ProgramKey: "missing", SubjectID: uuid.NewString(), EventKey: "store-wave7-missing-" + subjectID})
	if err != nil || len(unknown) != 1 || unknown[0].ErrorCode != "unknown_grant_program" {
		t.Fatalf("unknown grant program = %+v, error = %v", unknown, err)
	}
	invalidRegion, err := store.ExecuteGrantProgram(ctx, ExecuteGrantProgramRequest{Trigger: "manual", ProgramKey: "coverage_award", SubjectID: uuid.NewString(), EventKey: "store-wave7-region-" + subjectID, Region: "U$"})
	if err != nil || len(invalidRegion) != 1 || invalidRegion[0].ErrorCode != "invalid_request" {
		t.Fatalf("invalid grant region = %+v, error = %v", invalidRegion, err)
	}
}
