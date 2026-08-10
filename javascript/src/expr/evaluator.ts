import { Decimal } from "decimal.js";

import { ExpressionError } from "../errors.js";
import type { Node } from "./ast.js";

const ZERO = new Decimal(0);
const ONE = new Decimal(1);

function asDecimal(value: number | Decimal): Decimal {
  return value instanceof Decimal ? value : new Decimal(value);
}

function truthy(value: Decimal): boolean {
  return !value.isZero();
}

function requiredArgument(name: string, args: readonly Decimal[], index: number): Decimal {
  const argument = args[index];
  if (argument === undefined) {
    throw new ExpressionError(`${name}() received an invalid number of arguments`);
  }
  return argument;
}

function evaluateCall(name: string, args: Decimal[]): Decimal {
  switch (name) {
    case "ceil":
      return requiredArgument(name, args, 0).ceil();
    case "floor":
      return requiredArgument(name, args, 0).floor();
    case "min":
      return Decimal.min(...args);
    case "max":
      return Decimal.max(...args);
    case "round": {
      const digits = args.length === 2 ? requiredArgument(name, args, 1).toNumber() : 0;
      return requiredArgument(name, args, 0).toDecimalPlaces(digits, Decimal.ROUND_HALF_UP);
    }
    case "if":
      return truthy(requiredArgument(name, args, 0))
        ? requiredArgument(name, args, 1)
        : requiredArgument(name, args, 2);
    case "tier": {
      const value = requiredArgument(name, args, 0);
      for (let index = 1; index < args.length - 1; index += 2) {
        if (value.lessThan(requiredArgument(name, args, index))) {
          return requiredArgument(name, args, index + 1);
        }
      }
      return requiredArgument(name, args, args.length - 1);
    }
    case "clamp":
      return Decimal.max(
        requiredArgument(name, args, 1),
        Decimal.min(requiredArgument(name, args, 0), requiredArgument(name, args, 2)),
      );
    case "percentile":
      return percentile(args);
    default:
      throw new ExpressionError(`disallowed function: ${name}`);
  }
}

function percentile(args: Decimal[]): Decimal {
  const percentage = requiredArgument("percentile", args, 0);
  if (percentage.lessThan(0) || percentage.greaterThan(100)) {
    throw new ExpressionError("percentile() p must be between 0 and 100");
  }
  const sorted = args.slice(1).sort((left, right) => left.comparedTo(right));
  if (sorted.length === 1) return requiredArgument("percentile", sorted, 0);

  const rank = percentage.dividedBy(100).times(sorted.length - 1);
  const lower = rank.floor();
  const lowerIndex = lower.toNumber();
  const upperIndex = Math.min(lowerIndex + 1, sorted.length - 1);
  const fraction = rank.minus(lower);
  return requiredArgument("percentile", sorted, lowerIndex)
    .times(ONE.minus(fraction))
    .plus(requiredArgument("percentile", sorted, upperIndex).times(fraction));
}

export function evaluateNode(node: Node, variables: Record<string, number | Decimal>): Decimal {
  switch (node.type) {
    case "number":
      return new Decimal(node.value);
    case "identifier": {
      const value = variables[node.name];
      return value === undefined ? ZERO : asDecimal(value);
    }
    case "unary": {
      const value = evaluateNode(node.operand, variables);
      if (node.op === "not") return truthy(value) ? ZERO : ONE;
      return node.op === "-" ? value.negated() : value;
    }
    case "binary":
      return evaluateBinary(node.op, evaluateNode(node.left, variables), () =>
        evaluateNode(node.right, variables),
      );
    case "call":
      return evaluateCall(
        node.name,
        node.args.map((argument) => evaluateNode(argument, variables)),
      );
    case "ternary":
      return truthy(evaluateNode(node.cond, variables))
        ? evaluateNode(node.then, variables)
        : evaluateNode(node.else, variables);
    case "comparison":
      return evaluateComparison(
        node.op,
        evaluateNode(node.left, variables),
        evaluateNode(node.right, variables),
      );
    case "boolean": {
      const left = evaluateNode(node.left, variables);
      if (node.op === "and") {
        return truthy(left) ? evaluateNode(node.right, variables) : left;
      }
      if (node.op === "or") {
        return truthy(left) ? left : evaluateNode(node.right, variables);
      }
      throw new ExpressionError(`unknown boolean op: ${node.op}`);
    }
  }
}

function evaluateBinary(operator: string, left: Decimal, rightValue: () => Decimal): Decimal {
  const right = rightValue();
  switch (operator) {
    case "+":
      return left.plus(right);
    case "-":
      return left.minus(right);
    case "*":
      return left.times(right);
    case "/":
      if (right.isZero()) throw new ExpressionError("division by zero");
      return left.dividedBy(right);
    case "//":
      if (right.isZero()) throw new ExpressionError("division by zero");
      // Truncate toward zero to match Python's Decimal `//` (parity): e.g.
      // -7 // 2 == -3, not -4. decimal.js `.floor()` rounds toward -Infinity,
      // which would silently bill a different amount for negative operands.
      return left.divToInt(right);
    case "%":
      if (right.isZero()) throw new ExpressionError("modulo by zero");
      return left.modulo(right);
    default:
      throw new ExpressionError(`unknown operator: ${operator}`);
  }
}

function evaluateComparison(operator: string, left: Decimal, right: Decimal): Decimal {
  switch (operator) {
    case "==":
      return left.equals(right) ? ONE : ZERO;
    case "!=":
      return left.equals(right) ? ZERO : ONE;
    case "<":
      return left.lessThan(right) ? ONE : ZERO;
    case "<=":
      return left.lessThanOrEqualTo(right) ? ONE : ZERO;
    case ">":
      return left.greaterThan(right) ? ONE : ZERO;
    case ">=":
      return left.greaterThanOrEqualTo(right) ? ONE : ZERO;
    case "in":
      return left.toString().includes(right.toString()) ? ONE : ZERO;
    case "not in":
      return left.toString().includes(right.toString()) ? ZERO : ONE;
    default:
      throw new ExpressionError(`unknown comparison: ${operator}`);
  }
}
