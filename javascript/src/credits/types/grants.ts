import type { Decimal } from "decimal.js";
import type { CreditMetadata } from "./account.js";

export type GrantProgramTrigger =
  | "account_created"
  | "referral_completed"
  | "promo_code_redeemed"
  | "manual";

/** One application event that may award one or more catalog grants. */
export interface ExecuteGrantProgramRequest {
  trigger: GrantProgramTrigger;
  programKey: string;
  subjectId: string;
  eventKey: string;
  referrerSubjectId?: string | null;
  region?: string | null;
  metadata?: CreditMetadata | null;
}

interface GrantProgramAwardBase {
  replayed: boolean;
}

/** One committed award produced by a grant-program execution. */
export interface GrantProgramAwardSuccess extends GrantProgramAwardBase {
  grantEventId: string;
  grantAwardId: string;
  recipientSubjectId: string;
  ledgerEntryId: string;
  amount: Decimal;
  error: null;
}

/** A rejected grant-program execution. No financial identifiers are fabricated. */
export interface GrantProgramAwardFailure extends GrantProgramAwardBase {
  grantEventId: null;
  grantAwardId: null;
  recipientSubjectId: null;
  ledgerEntryId: null;
  amount: null;
  replayed: false;
  error: string;
}

export type GrantProgramAwardResult = GrantProgramAwardSuccess | GrantProgramAwardFailure;
