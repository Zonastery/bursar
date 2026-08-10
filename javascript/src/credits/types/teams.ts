import type { Decimal } from "decimal.js";

export type TeamRole = "owner" | "admin" | "member";

// ── Team/shared balance pools ─────────────────────────────────────────
/** A team with a shared credit balance pool. */
export interface Team {
  id: string;
  name: string;
  balance: Decimal;
  memberCount: number;
  createdAt: string;
}

/** A member of a team, with optional spend cap. */
export interface TeamMember {
  userId: string;
  role: TeamRole;
  spendCap: Decimal | null;
  totalSpent: Decimal;
}

/** Result of fetching team balance. */
export interface TeamBalanceResult {
  teamId: string;
  name: string;
  balance: Decimal;
  memberCount: number;
}

/** Result of creating a team. */
export interface CreateTeamResult {
  teamId: string;
  name: string;
  idempotent: boolean;
}

/** Result of adding a team member. */
export interface AddTeamMemberResult {
  teamId: string;
  userId: string;
  role: TeamRole;
}

/** Result of deducting credits from a team pool. */
interface TeamDeductionResultBase {
  teamId: string;
  userId: string;
  amount: Decimal;
  idempotent: boolean;
}

export interface TeamDeductionSuccess extends TeamDeductionResultBase {
  error: null;
  entryId: string | null;
  teamBalanceAfter: Decimal;
}

export interface TeamDeductionFailure extends TeamDeductionResultBase {
  error: string;
  entryId: null;
  teamBalanceAfter: Decimal | null;
  idempotent: false;
}

export type TeamDeductionResult = TeamDeductionSuccess | TeamDeductionFailure;
