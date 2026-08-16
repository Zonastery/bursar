import { z } from "zod";
import { Decimal } from "decimal.js";
import { StoreError } from "../../../errors.js";
import type { QueryFn } from "../../../shared/postgres-types.js";
import type { JsonObject, PostgresRow } from "../../../shared/json.js";
import type {
  BillingAutoRechargeAttempt,
  BillingAutoRechargeAttemptState,
  BillingAutoRechargeProfile,
} from "../../types/index.js";
import { optionalBoundedDiagnosticMessage } from "../../../shared/diagnostics.js";
import {
  optionalRecordRow,
  pgBoolean,
  postgresUuid,
  requireResultField,
  safeParse,
} from "../../../shared/postgres-validation.js";

const safeNonnegativeInteger = z
  .union([z.number(), z.string().regex(/^\d+$/)])
  .transform(Number)
  .pipe(z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER));
const positiveInteger = safeNonnegativeInteger.pipe(z.number().positive());
const timestamp = z
  .union([
    z.date().refine((value) => !Number.isNaN(value.getTime())),
    z.string().datetime({ offset: true }),
  ])
  .transform((value) => (value instanceof Date ? value : new Date(value)).toISOString());
const decimal = z
  .union([z.instanceof(Decimal), z.string().min(1), z.number().finite()])
  .transform((value) => new Decimal(value))
  .refine((value) => value.isFinite() && !value.isNegative(), "expected a non-negative decimal");

const ProfileRowSchema = z
  .object({
    subject_id: postgresUuid,
    enabled: pgBoolean,
    armed: pgBoolean,
    state: z.enum(["disabled", "active", "paused"]),
    provider: z.string().min(1).nullable(),
    topup_id: postgresUuid.nullable(),
    quantity: positiveInteger,
    threshold: decimal,
    max_charges_per_window: positiveInteger.nullable(),
    window_unit: z.enum(["second", "minute", "hour", "day", "week", "month", "year"]),
    window_count: positiveInteger,
    window_anchor: z.enum(["calendar", "rolling"]),
    window_timezone: z.string().min(1),
    updated_at: timestamp,
  })
  .strict()
  .superRefine((row, ctx) => {
    if (row.enabled !== (row.state !== "disabled")) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: "enabled and state are inconsistent" });
    }
    if (row.enabled && (row.provider === null || row.topup_id === null)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "enabled profile requires provider and topup_id",
      });
    }
  });

const AttemptStateSchema = z.enum([
  "claimed",
  "submitted",
  "processing",
  "unknown",
  "succeeded",
  "failed",
  "action_required",
]);
const AttemptRowSchema = z
  .object({
    id: postgresUuid,
    subject_id: postgresUuid,
    provider: z.string().min(1),
    idempotency_key: z.string().min(1),
    provider_attempt_id: z.string().min(1).nullable(),
    topup_id: postgresUuid,
    quantity: positiveInteger,
    state: AttemptStateSchema,
    window_start: timestamp,
    window_end: timestamp,
    quoted_amount_minor: safeNonnegativeInteger.nullable(),
    currency: z
      .string()
      .regex(/^[A-Z]{3}$/)
      .nullable(),
    failure_code: z.string().min(1).nullable(),
    failure_message: z.string().min(1).nullable(),
    metadata: z.record(z.string(), z.json()),
    created_at: timestamp,
    updated_at: timestamp,
  })
  .strict()
  .superRefine((row, ctx) => {
    if (new Date(row.window_end).getTime() <= new Date(row.window_start).getTime()) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "window_end must be later than window_start",
      });
    }
    if ((row.quoted_amount_minor === null) !== (row.currency === null)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "quoted_amount_minor and currency must be present together",
      });
    }
  });

type AttemptTransitionPath = readonly BillingAutoRechargeAttemptState[];

function attemptTransitions(
  entries: ReadonlyArray<readonly [BillingAutoRechargeAttemptState, AttemptTransitionPath]>,
): ReadonlyMap<BillingAutoRechargeAttemptState, AttemptTransitionPath> {
  return new Map(entries);
}

const ATTEMPT_TRANSITION_PATHS = new Map<
  BillingAutoRechargeAttemptState,
  ReadonlyMap<BillingAutoRechargeAttemptState, AttemptTransitionPath>
>([
  [
    "claimed",
    attemptTransitions([
      ["submitted", ["submitted"]],
      ["processing", ["submitted", "processing"]],
      ["succeeded", ["submitted", "processing", "succeeded"]],
      ["failed", ["submitted", "processing", "failed"]],
      ["unknown", ["submitted", "processing", "unknown"]],
      ["action_required", ["submitted", "action_required"]],
    ]),
  ],
  [
    "submitted",
    attemptTransitions([
      ["submitted", []],
      ["processing", ["processing"]],
      ["succeeded", ["processing", "succeeded"]],
      ["failed", ["processing", "failed"]],
      ["unknown", ["processing", "unknown"]],
      ["action_required", ["action_required"]],
    ]),
  ],
  [
    "processing",
    attemptTransitions([
      ["processing", []],
      ["succeeded", ["succeeded"]],
      ["failed", ["failed"]],
      ["unknown", ["unknown"]],
      ["action_required", ["action_required"]],
    ]),
  ],
  [
    "unknown",
    attemptTransitions([
      ["unknown", []],
      ["processing", ["processing"]],
      ["succeeded", ["succeeded"]],
      ["failed", ["failed"]],
      ["action_required", ["action_required"]],
    ]),
  ],
  [
    "action_required",
    attemptTransitions([
      ["action_required", []],
      ["processing", ["processing"]],
      ["succeeded", ["succeeded"]],
      ["failed", ["failed"]],
    ]),
  ],
]);

function projectProfile(row: PostgresRow) {
  return {
    subject_id: row.subject_id,
    enabled: row.enabled,
    armed: row.armed,
    state: row.state,
    provider: row.provider,
    topup_id: row.topup_id,
    quantity: row.quantity,
    threshold: row.threshold,
    max_charges_per_window: row.max_charges_per_window,
    window_unit: row.window_unit,
    window_count: row.window_count,
    window_anchor: row.window_anchor,
    window_timezone: row.window_timezone,
    updated_at: row.updated_at,
  };
}

function projectAttempt(row: PostgresRow) {
  return {
    id: row.id,
    subject_id: row.subject_id,
    provider: row.provider,
    idempotency_key: row.idempotency_key,
    provider_attempt_id: row.provider_attempt_id,
    topup_id: row.topup_id,
    quantity: row.quantity,
    state: row.state,
    window_start: row.window_start,
    window_end: row.window_end,
    quoted_amount_minor: row.quoted_amount_minor,
    currency: row.currency,
    failure_code: row.failure_code,
    failure_message: row.failure_message,
    metadata: row.metadata,
    created_at: row.created_at,
    updated_at: row.updated_at,
  };
}

function profileFromRow(row: PostgresRow): BillingAutoRechargeProfile {
  const parsed = safeParse(
    ProfileRowSchema,
    projectProfile(row),
    "BillingAutoRechargeRepository.profile",
  );
  return {
    userId: parsed.subject_id,
    enabled: parsed.enabled,
    armed: parsed.armed,
    state: parsed.state,
    provider: parsed.provider,
    topupId: parsed.topup_id,
    quantity: parsed.quantity,
    threshold: parsed.threshold,
    maxChargesPerWindow: parsed.max_charges_per_window,
    windowUnit: parsed.window_unit,
    windowCount: parsed.window_count,
    windowAnchor: parsed.window_anchor,
    windowTimezone: parsed.window_timezone,
    updatedAt: parsed.updated_at,
  };
}

function attemptFromRow(row: PostgresRow): BillingAutoRechargeAttempt {
  const parsed = safeParse(
    AttemptRowSchema,
    projectAttempt(row),
    "BillingAutoRechargeRepository.attempt",
  );
  return {
    id: parsed.id,
    userId: parsed.subject_id,
    provider: parsed.provider,
    idempotencyKey: parsed.idempotency_key,
    providerAttemptId: parsed.provider_attempt_id,
    topupId: parsed.topup_id,
    quantity: parsed.quantity,
    state: parsed.state,
    windowStart: parsed.window_start,
    windowEnd: parsed.window_end,
    quotedAmountMinor: parsed.quoted_amount_minor,
    currency: parsed.currency,
    failureCode: parsed.failure_code,
    failureMessage: parsed.failure_message,
    metadata: parsed.metadata,
    createdAt: parsed.created_at,
    updatedAt: parsed.updated_at,
  };
}

export class BillingAutoRechargeRepository {
  constructor(private readonly query: QueryFn) {}

  async getProfile(userId: string): Promise<BillingAutoRechargeProfile | null> {
    const rows = await this.query("SELECT * FROM bursar.get_auto_recharge_profile($1::uuid)", [
      userId,
    ]);
    const row = optionalRecordRow(rows, "BillingAutoRechargeRepository.getProfile");
    return row === null ? null : profileFromRow(row);
  }

  async upsertProfile(
    profile: BillingAutoRechargeProfile,
    options: { resetCooldown?: boolean } = {},
  ): Promise<void> {
    const rows = await this.query(
      `SELECT bursar.upsert_auto_recharge_profile(
         $1::uuid, $2, $3, $4::uuid, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14
       ) AS updated`,
      [
        profile.userId,
        profile.enabled,
        profile.provider,
        profile.topupId,
        profile.quantity,
        profile.threshold.toString(),
        profile.maxChargesPerWindow,
        profile.windowUnit,
        profile.windowCount,
        profile.windowAnchor,
        profile.windowTimezone,
        profile.armed,
        profile.state,
        options.resetCooldown ?? false,
      ],
    );
    if (
      !requireResultField(rows, "updated", pgBoolean, "BillingAutoRechargeRepository.upsertProfile")
    ) {
      throw new StoreError(`auto-recharge profile update rejected: ${profile.userId}`);
    }
  }

  async claimAttempt(input: {
    userId: string;
    idempotencyKey: string;
  }): Promise<BillingAutoRechargeAttempt | null> {
    const rows = await this.query(
      "SELECT * FROM bursar.claim_auto_recharge_attempt($1::uuid, $2)",
      [input.userId, input.idempotencyKey],
    );
    const row = optionalRecordRow(rows, "BillingAutoRechargeRepository.claimAttempt");
    return row === null ? null : attemptFromRow(row);
  }

  private async advanceAttempt(input: {
    id: string;
    state: BillingAutoRechargeAttemptState;
    providerAttemptId?: string | null;
    failureCode?: string | null;
    failureMessage?: string | null;
    metadata?: JsonObject;
  }): Promise<void> {
    const failureMessage = optionalBoundedDiagnosticMessage(input.failureMessage);
    const currentRows = await this.query(
      "SELECT * FROM bursar.get_auto_recharge_attempt($1::uuid)",
      [input.id],
    );
    const currentRow = optionalRecordRow(
      currentRows,
      "BillingAutoRechargeRepository.advanceAttempt.current",
    );
    if (currentRow === null) {
      throw new StoreError(`auto-recharge attempt not found: ${input.id}`, {
        details: { attemptId: input.id },
      });
    }
    const current = safeParse(
      AttemptStateSchema,
      currentRow.state,
      "BillingAutoRechargeRepository.advanceAttempt.current.state",
    );
    const path = ATTEMPT_TRANSITION_PATHS.get(current)?.get(input.state);
    if (!path) {
      throw new StoreError(`auto-recharge attempt transition rejected: ${input.id}`, {
        details: { attemptId: input.id, currentState: current, requestedState: input.state },
      });
    }
    for (const state of path) {
      const rows = await this.query(
        `SELECT bursar.advance_auto_recharge_attempt(
         $1::uuid,
         $2::bursar.recharge_attempt_status,
         $3,
         $4,
         $5,
         $6::jsonb
       ) AS advanced`,
        [
          input.id,
          state,
          input.providerAttemptId ?? null,
          input.failureCode ?? null,
          failureMessage,
          JSON.stringify(input.metadata ?? {}),
        ],
      );
      if (
        !requireResultField(
          rows,
          "advanced",
          pgBoolean,
          "BillingAutoRechargeRepository.advanceAttempt",
        )
      ) {
        throw new StoreError(`auto-recharge attempt transition rejected: ${input.id}`, {
          details: { attemptId: input.id, requestedState: state },
        });
      }
    }
  }

  async updateAttempt(input: {
    id: string;
    state: BillingAutoRechargeAttemptState;
    providerAttemptId?: string | null;
    failureCode?: string | null;
    failureMessage?: string | null;
    metadata?: JsonObject;
  }): Promise<void> {
    return this.advanceAttempt(input);
  }

  async updateAttemptByProviderPayment(input: {
    provider: string;
    providerPaymentId: string;
    state: BillingAutoRechargeAttemptState;
    failureCode?: string | null;
    failureMessage?: string | null;
  }): Promise<void> {
    const rows = await this.query(
      `SELECT * FROM bursar.get_auto_recharge_attempt_by_provider($1, $2)`,
      [input.provider, input.providerPaymentId],
    );
    const row = optionalRecordRow(
      rows,
      "BillingAutoRechargeRepository.updateAttemptByProviderPayment",
    );
    if (row === null) return;
    const attempt = attemptFromRow(row);
    await this.advanceAttempt({
      id: attempt.id,
      state: input.state,
      providerAttemptId: input.providerPaymentId,
      failureCode: input.failureCode,
      failureMessage: input.failureMessage,
    });
  }

  async countAttempts(userId: string, since: string | Date): Promise<number> {
    const sinceDate = since instanceof Date ? since : new Date(since);
    if (Number.isNaN(sinceDate.getTime())) {
      throw new TypeError("auto-recharge attempt boundary must be a valid instant");
    }
    const rows = await this.query(
      `SELECT bursar.count_auto_recharge_attempts($1::uuid, $2::timestamptz) AS count`,
      [userId, sinceDate.toISOString()],
    );
    return requireResultField(
      rows,
      "count",
      z
        .union([z.number(), z.string().regex(/^\d+$/)])
        .transform(Number)
        .pipe(z.number().int().nonnegative().safe()),
      "BillingAutoRechargeRepository.countAttempts",
    );
  }
}
