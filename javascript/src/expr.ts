import Decimal from "decimal.js";

import { ExpressionError } from "./errors.js";
import { children, type Node } from "./expr/ast.js";
import { evaluateNode } from "./expr/evaluator.js";
import { ALLOWED_FUNCTIONS, collectVariables, validateCalls } from "./expr/language.js";
import { ExpressionParser } from "./expr/parser.js";

export type * from "./expr/ast.js";

const MONEY_DECIMAL_PLACES = 6;

/** Quantize a Decimal credit amount to 6dp using ROUND_HALF_UP. */
export function quantizeMoney(value: Decimal): Decimal {
  return value.toDecimalPlaces(MONEY_DECIMAL_PLACES, Decimal.ROUND_HALF_UP);
}

function parseExpression(source: string, trailingTokenMessage: string): Node {
  const parser = ExpressionParser.fromSource(source);
  const node = parser.parse();
  if (!parser.isAtEnd()) {
    throw new ExpressionError(
      trailingTokenMessage.replace("{token}", parser.peek()?.value ?? "EOF"),
    );
  }
  return node;
}

/**
 * Validate that an expression is safe, syntactically valid, and refers only
 * to the supplied set of known metric variables.
 */
export function validateExpression(expression: string, knownVariables: Iterable<string>): void {
  try {
    const node = parseExpression(expression, "unexpected token after expression: '{token}'");
    validateCalls(node);
    const variables = collectVariables(node);
    if (variables.size === 0) {
      throw new ExpressionError(
        "expression references no variables -- must use at least one metric",
      );
    }
    const known = knownVariables instanceof Set ? knownVariables : new Set(knownVariables);
    for (const name of variables) {
      if (!known.has(name)) throw new ExpressionError(`unknown variable: '${name}'`);
    }
  } catch (error) {
    if (error instanceof ExpressionError) throw error;
    throw new ExpressionError(`invalid expression: ${(error as Error).message}`, { cause: error });
  }
}

/**
 * Safely evaluate a validated expression using exact Decimal arithmetic.
 */
export function evaluateExpression(
  expression: string,
  variables: Record<string, number | Decimal>,
): Decimal {
  if (!variables || typeof variables !== "object") {
    throw new ExpressionError("variables must be a dict");
  }
  if (Object.keys(variables).length === 0) {
    throw new ExpressionError("cannot evaluate: variables dict is empty");
  }

  let node: Node;
  try {
    node = parseExpression(expression, "unexpected token: '{token}'");
  } catch (error) {
    if (error instanceof ExpressionError) throw error;
    throw new ExpressionError(`syntax error: ${(error as Error).message}`, { cause: error });
  }
  validateCalls(node);
  validateVariables(node, variables);

  const result = evaluateNode(node, variables);
  if (!result.isFinite()) {
    throw new ExpressionError(`expression evaluated to a non-finite value: ${result.toString()}`);
  }
  return result;
}

function validateVariables(node: Node, variables: Record<string, number | Decimal>): void {
  if (
    node.type === "identifier" &&
    !ALLOWED_FUNCTIONS.has(node.name) &&
    !Object.prototype.hasOwnProperty.call(variables, node.name)
  ) {
    throw new ExpressionError(`undefined variable: '${node.name}'`);
  }
  for (const child of children(node)) validateVariables(child, variables);
}
