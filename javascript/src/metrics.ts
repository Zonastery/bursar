/** A single tool invocation recorded during an agent step. */
export interface ToolCall {
  name: string;
}

/** Raw usage counters collected across one or more agent steps. */
export interface UsageMetrics {
  model?: string | null;
  inputTokens?: number;
  outputTokens?: number;
  cacheReadTokens?: number;
  cacheWriteTokens?: number;
  toolCalls?: ToolCall[];
  searchQueries?: number;
  searchResults?: number;
  webSearchCalls?: number;
  codeExecCalls?: number;
  fixedJob?: string | null;
}
