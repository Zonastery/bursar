import type { PostgresParams, PostgresRow, PostgresValue } from "./json.js";

export interface QueryFn {
  (text: string, params?: PostgresParams): Promise<PostgresRow[]>;
}

export interface CallProc {
  (name: string, params: PostgresParams): Promise<PostgresValue[]>;
}
