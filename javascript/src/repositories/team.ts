import { z } from "zod";
import type { CallProc } from "./types.js";

export const CreateTeamRowSchema = z
  .object({
    team_id: z.string().optional(),
    name: z.string().optional(),
  })
  .passthrough();

export const TeamBalanceRowSchema = z
  .object({
    team_id: z.string().optional(),
    name: z.string().optional(),
    balance: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    member_count: z.number().optional(),
    error: z.string().optional(),
  })
  .passthrough();

export const AddTeamMemberRowSchema = z
  .object({
    team_id: z.string().optional(),
    user_id: z.string().optional(),
    role: z.string().optional(),
  })
  .passthrough();

export const TeamMemberRowSchema = z
  .object({
    user_id: z.string().optional(),
    role: z.string().optional(),
    spend_cap: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    total_spent: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
  })
  .passthrough();

export const TeamDeductionRowSchema = z
  .object({
    transaction_id: z.string().optional(),
    team_id: z.string().optional(),
    user_id: z.string().optional(),
    amount: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    team_balance_after: z
      .union([z.string(), z.number()] as const)
      .nullable()
      .optional(),
    error: z.string().optional(),
  })
  .passthrough();

export type CreateTeamRow = z.infer<typeof CreateTeamRowSchema>;
export type TeamBalanceRow = z.infer<typeof TeamBalanceRowSchema>;
export type AddTeamMemberRow = z.infer<typeof AddTeamMemberRowSchema>;
export type TeamMemberRow = z.infer<typeof TeamMemberRowSchema>;
export type TeamDeductionRow = z.infer<typeof TeamDeductionRowSchema>;

export class TeamRepository {
  constructor(private callproc: CallProc) {}

  async createTeam(name: string, initialBalance: string): Promise<CreateTeamRow> {
    const rows = await this.callproc("create_team", [name, initialBalance]);
    return CreateTeamRowSchema.parse(rows?.[0] ?? {});
  }

  async getTeamBalance(teamId: string): Promise<TeamBalanceRow | null> {
    const rows = await this.callproc("get_team_balance", [teamId]);
    if (!rows || rows.length === 0) return null;
    return TeamBalanceRowSchema.parse(rows[0]);
  }

  async addTeamMember(
    teamId: string,
    userId: string,
    role: string,
    spendCap: string | null,
  ): Promise<AddTeamMemberRow> {
    const rows = await this.callproc("add_team_member", [teamId, userId, role, spendCap]);
    return AddTeamMemberRowSchema.parse(rows?.[0] ?? {});
  }

  async getTeamMembers(teamId: string): Promise<TeamMemberRow[]> {
    const rows = await this.callproc("get_team_members", [teamId]);
    return (rows ?? []).map((r) => TeamMemberRowSchema.parse(r));
  }

  async deductTeam(
    teamId: string,
    userId: string,
    amount: string,
    metadata: string,
  ): Promise<TeamDeductionRow> {
    const rows = await this.callproc("deduct_team", [teamId, userId, amount, metadata]);
    return TeamDeductionRowSchema.parse(rows?.[0] ?? {});
  }
}
