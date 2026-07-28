export interface QueryFn {
  (text: string, params?: unknown[]): Promise<unknown[]>;
}

export interface CallProc {
  (name: string, params: unknown[]): Promise<unknown[]>;
}
