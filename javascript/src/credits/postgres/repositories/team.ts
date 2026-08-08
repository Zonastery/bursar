import { z } from "zod";
import Decimal from "decimal.js";
import type { CallProc } from "../../../shared/postgres-types.js";
import {
  optionalRecordRow,
  postgresUuid,
  requireRow,
  safeParse,
} from "../../../shared/postgres-validation.js";
import { StoreError } from "../../../errors.js";

const decimal = z.union([z.string().min(1), z.number().finite()] as const);
const safeInteger = z
  .union([z.number().int(), z.string().regex(/^\d+$/)] as const)
  .transform(Number)
  .refine(Number.isSafeInteger, "expected a safe integer");
const teamRole = z.enum(["owner", "admin", "member"]);

const CreateTeamRowSchema = z
  .object({
    team_id: postgresUuid.nullable(),
    name: z.string().min(1).nullable(),
    team_subject_id: postgresUuid.nullable(),
    account_id: postgresUuid.nullable(),
    error_code: z.string().min(1).nullable(),
  })
  .strict()
  .superRefine((row, context) => {
    const identifiers = [row.team_id, row.name, row.team_subject_id, row.account_id];
    if (row.error_code === null && identifiers.some((value) => value === null)) {
      context.addIssue({
        code: "custom",
        message: "successful team creation requires identity fields",
      });
    }
    if (row.error_code !== null && identifiers.some((value) => value !== null)) {
      context.addIssue({
        code: "custom",
        message: "failed team creation cannot expose identity fields",
      });
    }
  });

const TeamBalanceRowSchema = z
  .object({
    team_id: postgresUuid,
    name: z.string().min(1),
    balance: decimal,
    member_count: safeInteger,
  })
  .strict();

const AddTeamMemberRowSchema = z
  .object({
    team_id: postgresUuid,
    user_id: postgresUuid,
    role: teamRole,
  })
  .strict();

const TeamMemberRowSchema = z
  .object({
    user_id: postgresUuid,
    role: teamRole,
    spend_cap: decimal.nullable(),
    total_spent: decimal,
  })
  .strict();

const TeamDeductionRpcRowSchema = z
  .object({
    entry_id: postgresUuid.nullable(),
    team_id: postgresUuid,
    subject_id: postgresUuid,
    amount: decimal,
    balance_after: decimal.nullable(),
    replayed: z.boolean(),
    error_code: z.string().min(1).nullable(),
  })
  .strict();

const TeamDeductionRowSchema = z
  .object({
    entry_id: postgresUuid.nullable(),
    team_id: postgresUuid,
    user_id: postgresUuid,
    amount: decimal,
    team_balance_after: decimal.nullable(),
    error: z.string().min(1).nullable(),
    replayed: z.boolean(),
  })
  .strict()
  .superRefine((row, context) => {
    if (row.error === null && (row.entry_id == null || row.team_balance_after === null)) {
      context.addIssue({
        code: "custom",
        message: "successful team deductions require entry and balance fields",
      });
    }
    if (row.error !== null && (row.entry_id !== null || row.replayed)) {
      context.addIssue({
        code: "custom",
        message: "failed team deductions cannot be replayed or committed",
      });
    }
  });

export type CreateTeamRow = z.infer<typeof CreateTeamRowSchema>;
export type TeamBalanceRow = z.infer<typeof TeamBalanceRowSchema>;
export type AddTeamMemberRow = z.infer<typeof AddTeamMemberRowSchema>;
export type TeamMemberRow = z.infer<typeof TeamMemberRowSchema>;
export type TeamDeductionRow = z.infer<typeof TeamDeductionRowSchema>;

/** Repository for team credit operations.
 *
 * All methods call Postgres RPCs via the callproc function.
 * Returns None when the RPC returns no rows.
 * Returns typed models for successful results.
 */
export class TeamRepository {
  constructor(private callproc: CallProc) {}

  /** Create a new team with an initial balance. */
  async createTeam(
    ownerSubjectId: string,
    name: string,
    initialBalance: string,
  ): Promise<CreateTeamRow> {
    const rows = await this.callproc("create_team", [ownerSubjectId, name, initialBalance]);
    return safeParse(
      CreateTeamRowSchema,
      requireRow(rows, "TeamRepository.createTeam"),
      "TeamRepository.createTeam",
    );
  }

  /** Fetch a team's balance and member count. Returns null if the team does not exist. */
  async getTeamBalance(teamId: string): Promise<TeamBalanceRow | null> {
    const rows = await this.callproc("get_team_balance", [teamId]);
    const row = optionalRecordRow(rows, "TeamRepository.getTeamBalance");
    return row === null
      ? null
      : safeParse(TeamBalanceRowSchema, row, "TeamRepository.getTeamBalance");
  }

  /** Add a member to a team with an optional spend cap. */
  async addTeamMember(
    teamId: string,
    userId: string,
    role: string,
    spendCap: string | null,
  ): Promise<AddTeamMemberRow> {
    const rows = await this.callproc("set_team_member", [teamId, userId, role, spendCap]);
    const added = safeParse(
      z.boolean(),
      requireRow(rows, "TeamRepository.addTeamMember"),
      "TeamRepository.addTeamMember",
    );
    if (!added) {
      throw new StoreError("TeamRepository.addTeamMember: set_team_member returned false");
    }
    return safeParse(
      AddTeamMemberRowSchema,
      { team_id: teamId, user_id: userId, role },
      "TeamRepository.addTeamMember",
    );
  }

  /** List all members of a team with their spend caps and totals. */
  async getTeamMembers(teamId: string): Promise<TeamMemberRow[]> {
    const rows = await this.callproc("list_team_members", [teamId]);
    return (rows ?? []).map((r) =>
      safeParse(TeamMemberRowSchema, r, "TeamRepository.getTeamMembers"),
    );
  }

  /** Remove a team member, returning false when absent or the final owner. */
  async removeTeamMember(teamId: string, userId: string): Promise<boolean> {
    const rows = await this.callproc("remove_team_member", [teamId, userId]);
    return safeParse(
      z.boolean(),
      requireRow(rows, "TeamRepository.removeTeamMember"),
      "TeamRepository.removeTeamMember",
    );
  }

  /** Deduct credits from a team's balance on behalf of a user. */
  async deductTeam(
    teamId: string,
    userId: string,
    amount: string,
    idempotencyKey: string,
    operation: string,
    metadata: string,
  ): Promise<TeamDeductionRow> {
    const rows = await this.callproc("deduct_team", [
      teamId,
      userId,
      amount,
      idempotencyKey,
      operation,
      metadata,
    ]);
    const row = safeParse(
      TeamDeductionRpcRowSchema,
      requireRow(rows, "TeamRepository.deductTeam"),
      "TeamRepository.deductTeam",
    );
    if (row.error_code === null && !new Decimal(row.amount).equals(amount)) {
      throw new StoreError("TeamRepository.deductTeam: committed amount differs from the request", {
        indeterminate: true,
      });
    }
    return safeParse(
      TeamDeductionRowSchema,
      {
        entry_id: row.entry_id,
        team_id: row.team_id,
        user_id: row.subject_id,
        amount,
        team_balance_after: row.balance_after,
        replayed: row.replayed,
        error: row.error_code,
      },
      "TeamRepository.deductTeam",
    );
  }
}
