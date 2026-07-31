import type { Decimal } from "decimal.js";
import type { CreditMetadata } from "./account.js";

export type GrantProgramTrigger =
  "account_created" | "referral_completed" | "promo_code_redeemed" | "manual";

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

/** One award row produced by a grant-program execution. */
export interface GrantProgramAwardResult {
  grantEventId: string | null;
  grantAwardId: string | null;
  recipientSubjectId: string | null;
  ledgerEntryId: string | null;
  amount: Decimal;
  replayed: boolean;
  error?: string | null;
}
