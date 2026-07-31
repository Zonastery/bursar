import { ExpressionError } from "../errors.js";
import { children, type Node } from "./ast.js";

export const ALLOWED_FUNCTIONS = new Set([
  "ceil",
  "floor",
  "min",
  "max",
  "round",
  "if",
  "tier",
  "clamp",
  "percentile",
]);

function checkCallArity(name: string, argc: number): void {
  switch (name) {
    case "if":
      if (argc !== 3) {
        throw new ExpressionError("if() requires exactly 3 arguments: if(condition, then, else)");
      }
      return;
    case "clamp":
      if (argc !== 3) {
        throw new ExpressionError("clamp() requires exactly 3 arguments: clamp(x, min, max)");
      }
      return;
    case "tier":
      if (argc < 4 || argc % 2 !== 0) {
        throw new ExpressionError(
          "tier() requires an even number of arguments >= 4 (value, threshold, rate, ..., default)",
        );
      }
      return;
    case "percentile":
      if (argc < 2) {
        throw new ExpressionError("percentile() requires at least 2 arguments (p, v1, [v2, ...])");
      }
      return;
    case "min":
    case "max":
      if (argc < 1) throw new ExpressionError(`${name}() requires at least 1 argument`);
      return;
    case "ceil":
    case "floor":
      if (argc !== 1) throw new ExpressionError(`${name}() requires exactly 1 argument`);
      return;
    case "round":
      if (argc !== 1 && argc !== 2) {
        throw new ExpressionError("round() requires 1 or 2 arguments: round(x[, ndigits])");
      }
      return;
    default:
      return;
  }
}

export function validateCalls(node: Node): void {
  if (node.type === "call") checkCallArity(node.name, node.args.length);
  for (const child of children(node)) validateCalls(child);
}

export function collectVariables(node: Node): Set<string> {
  const variables = new Set<string>();
  const visit = (current: Node): void => {
    if (current.type === "identifier" && !ALLOWED_FUNCTIONS.has(current.name)) {
      variables.add(current.name);
    }
    for (const child of children(current)) visit(child);
  };
  visit(node);
  return variables;
}
