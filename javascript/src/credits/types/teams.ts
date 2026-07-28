import type { Decimal } from "decimal.js";

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
  role: string;
  spendCap?: Decimal | null;
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
}

/** Result of adding a team member. */
export interface AddTeamMemberResult {
  teamId: string;
  userId: string;
  role: string;
}

/** Result of deducting credits from a team pool. */
export interface TeamDeductionResult {
  entryId: string;
  teamId: string;
  userId: string;
  amount: Decimal;
  teamBalanceAfter: Decimal;
  error?: string | null;
}
