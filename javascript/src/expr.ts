import Decimal from "decimal.js";
import { ExpressionError } from "./errors.js";

// Quantization: all credit amounts round to 4 decimal places, ROUND_HALF_UP.
// This must match the Python SDK's Decimal quantize(0.0001, ROUND_HALF_UP).
export const MONEY_DP = 4;
export const MONEY_ROUNDING = Decimal.ROUND_HALF_UP;

/** Quantize a Decimal credit amount to 4dp, ROUND_HALF_UP. */
export function quantizeMoney(value: Decimal): Decimal {
  return value.toDecimalPlaces(MONEY_DP, MONEY_ROUNDING);
}

const ALLOWED_FUNCTIONS = new Set([
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

// ── Tokenizer ──────────────────────────────────────────────────────────────

type TokenType =
  | "number"
  | "identifier"
  | "+"
  | "-"
  | "*"
  | "/"
  | "//"
  | "%"
  | "**"
  | "("
  | ")"
  | ","
  | "=="
  | "!="
  | "<"
  | "<="
  | ">"
  | ">="
  | "in"
  | "not"
  | "and"
  | "or"
  | "if"
  | "else";

interface Token {
  type: TokenType;
  value: string;
}

function tokenize(src: string): Token[] {
  const tokens: Token[] = [];
  let i = 0;
  while (i < src.length) {
    if (src[i] === " " || src[i] === "\t" || src[i] === "\n") {
      i++;
      continue;
    }
    if (src[i] === "(") {
      tokens.push({ type: "(", value: "(" });
      i++;
      continue;
    }
    if (src[i] === ")") {
      tokens.push({ type: ")", value: ")" });
      i++;
      continue;
    }
    if (src[i] === ",") {
      tokens.push({ type: ",", value: "," });
      i++;
      continue;
    }

    // Two-char operators
    const two = src.slice(i, i + 2);
    if (two === "**") {
      tokens.push({ type: "**", value: "**" });
      i += 2;
      continue;
    }
    if (two === "//") {
      tokens.push({ type: "//", value: "//" });
      i += 2;
      continue;
    }
    if (two === "==") {
      tokens.push({ type: "==", value: "==" });
      i += 2;
      continue;
    }
    if (two === "!=") {
      tokens.push({ type: "!=", value: "!=" });
      i += 2;
      continue;
    }
    if (two === "<=") {
      tokens.push({ type: "<=", value: "<=" });
      i += 2;
      continue;
    }
    if (two === ">=") {
      tokens.push({ type: ">=", value: ">=" });
      i += 2;
      continue;
    }

    // "not in" needs special handling — we tokenize "not" and then "in" separately
    // The parser will handle "not" + "in" as "not in"

    // One-char operators
    if (src[i] === "+") {
      tokens.push({ type: "+", value: "+" });
      i++;
      continue;
    }
    if (src[i] === "-") {
      tokens.push({ type: "-", value: "-" });
      i++;
      continue;
    }
    if (src[i] === "*") {
      tokens.push({ type: "*", value: "*" });
      i++;
      continue;
    }
    if (src[i] === "/") {
      tokens.push({ type: "/", value: "/" });
      i++;
      continue;
    }
    if (src[i] === "%") {
      tokens.push({ type: "%", value: "%" });
      i++;
      continue;
    }
    if (src[i] === "<") {
      tokens.push({ type: "<", value: "<" });
      i++;
      continue;
    }
    if (src[i] === ">") {
      tokens.push({ type: ">", value: ">" });
      i++;
      continue;
    }

    // Numbers — strict literal: digits with optional '.' and optional 'e'/'E' exponent.
    // Supports scientific notation (1e3, 1e-6, 2.5E+10) so per-token pricing
    // configs like {"_default":"input_tokens * 1e-6"} load in JS (H5 parity fix).
    // Rejects malformed literals like "1.2.3" (H11 parity fix).
    if (/[0-9.]/.test(src[i])) {
      let num = "";
      let dotSeen = false;
      // Mantissa: digits and at most one '.'
      while (i < src.length && /[0-9.]/.test(src[i])) {
        if (src[i] === ".") {
          if (dotSeen) {
            throw new ExpressionError(`invalid number literal: '${num}.'`);
          }
          dotSeen = true;
        }
        num += src[i];
        i++;
      }
      if (num === "." || !/^[0-9]*\.?[0-9]*$/.test(num) || !/[0-9]/.test(num)) {
        throw new ExpressionError(`invalid number literal: '${num}'`);
      }
      // Optional exponent: e/E followed by optional +/- and one or more digits.
      if (i < src.length && (src[i] === "e" || src[i] === "E")) {
        num += src[i];
        i++;
        if (i < src.length && (src[i] === "+" || src[i] === "-")) {
          num += src[i];
          i++;
        }
        if (i >= src.length || !/[0-9]/.test(src[i])) {
          throw new ExpressionError(`invalid number literal: '${num}'`);
        }
        while (i < src.length && /[0-9]/.test(src[i])) {
          num += src[i];
          i++;
        }
      }
      tokens.push({ type: "number", value: num });
      continue;
    }

    // Identifiers and keywords
    if (/[a-zA-Z_]/.test(src[i])) {
      let word = "";
      while (i < src.length && /[a-zA-Z0-9_]/.test(src[i])) {
        word += src[i];
        i++;
      }
      const kw: TokenType =
        word === "and"
          ? "and"
          : word === "or"
            ? "or"
            : word === "if"
              ? "if"
              : word === "else"
                ? "else"
                : word === "in"
                  ? "in"
                  : word === "not"
                    ? "not"
                    : word === "true"
                      ? "number"
                      : word === "false"
                        ? "number"
                        : "identifier";
      tokens.push({
        type: kw,
        value: word === "true" ? "1" : word === "false" ? "0" : word,
      });
      continue;
    }

    throw new ExpressionError(`unexpected character: '${src[i]}'`);
  }
  return tokens;
}

// ── AST nodes ──────────────────────────────────────────────────────────────

export interface NumNode {
  type: "number";
  // Exact string form of the literal; parsed via new Decimal(value) so we never
  // round-trip through binary float.
  value: string;
}
export interface IdentNode {
  type: "identifier";
  name: string;
}
export interface BinOpNode {
  type: "binary";
  op: string;
  left: Node;
  right: Node;
}
export interface UnaryNode {
  type: "unary";
  op: string;
  operand: Node;
}
export interface CallNode {
  type: "call";
  name: string;
  args: Node[];
}
export interface TernaryNode {
  type: "ternary";
  cond: Node;
  then: Node;
  else: Node;
}
export interface CompareNode {
  type: "comparison";
  op: string;
  left: Node;
  right: Node;
}
export interface BoolOpNode {
  type: "boolean";
  op: string;
  left: Node;
  right: Node;
}

export type Node =
  | NumNode
  | IdentNode
  | BinOpNode
  | UnaryNode
  | CallNode
  | TernaryNode
  | CompareNode
  | BoolOpNode;

// ── Parser ─────────────────────────────────────────────────────────────────

class Parser {
  private tokens: Token[];
  private pos = 0;

  constructor(tokens: Token[]) {
    this.tokens = tokens;
  }

  peek(): Token | undefined {
    return this.tokens[this.pos];
  }
  private previous(): Token {
    return this.tokens[this.pos - 1];
  }
  isAtEnd(): boolean {
    return this.pos >= this.tokens.length;
  }

  private check(...types: TokenType[]): boolean {
    if (this.isAtEnd()) return false;
    return types.includes(this.tokens[this.pos].type);
  }

  private match(...types: TokenType[]): boolean {
    for (const t of types) {
      if (this.check(t)) {
        this.pos++;
        return true;
      }
    }
    return false;
  }

  private consume(type: TokenType, msg: string): Token {
    if (this.check(type)) return this.tokens[this.pos++];
    throw new ExpressionError(msg);
  }

  parse(): Node {
    const expr = this.boolExpr();

    // Handle ternary: X if cond else Y (at top level so operators bind correctly)
    if (this.match("if")) {
      const cond = this.boolExpr();
      this.consume("else", "expected 'else' in ternary expression");
      const elseBranch = this.boolExpr();
      return { type: "ternary", cond, then: expr, else: elseBranch } as TernaryNode;
    }

    return expr;
  }

  private comparison(): Node {
    let left = this.addition();
    // Handle "not in" as two tokens
    if (this.match("not") && this.match("in")) {
      const right = this.addition();
      left = { type: "comparison", op: "not in", left, right } as CompareNode;
    }
    while (this.match("==", "!=", "<", "<=", ">", ">=", "in")) {
      const op = this.previous().value;
      const right = this.addition();
      left = { type: "comparison", op, left, right } as CompareNode;
    }
    return left;
  }

  private addition(): Node {
    let left = this.multiplication();
    while (this.match("+", "-")) {
      const op = this.previous().value;
      const right = this.multiplication();
      left = { type: "binary", op, left, right } as BinOpNode;
    }
    return left;
  }

  private multiplication(): Node {
    let left = this.unary();
    // NOTE: "**" is intentionally NOT accepted here. Exponentiation is rejected
    // entirely (sandbox decision C5/C7, matching the Python SDK) — a "**" token
    // surfaces as a leftover token and fails validation/evaluation. We also
    // raise an explicit error if one is seen mid-expression.
    while (this.match("*", "/", "//", "%")) {
      const op = this.previous().value;
      const right = this.unary();
      left = { type: "binary", op, left, right } as BinOpNode;
    }
    if (this.check("**")) {
      throw new ExpressionError(
        "exponentiation operator '**' is not allowed in pricing expressions",
      );
    }
    return left;
  }

  private notExpr(): Node {
    if (this.match("not")) {
      const operand = this.notExpr();
      return { type: "unary", op: "not", operand } as UnaryNode;
    }
    return this.comparison();
  }

  private boolExpr(): Node {
    let left = this.notExpr();
    while (this.match("and", "or")) {
      const op = this.previous().value;
      const right = this.notExpr();
      left = { type: "boolean", op, left, right } as BoolOpNode;
    }
    return left;
  }

  private unary(): Node {
    if (this.match("+", "-")) {
      const op = this.previous().value;
      return { type: "unary", op, operand: this.unary() } as UnaryNode;
    }
    return this.callOrPrimary();
  }

  private callOrPrimary(): Node {
    return this.primary();
  }

  private primary(): Node {
    if (this.check("**")) {
      throw new ExpressionError(
        "exponentiation operator '**' is not allowed in pricing expressions",
      );
    }
    if (this.match("number")) {
      return { type: "number", value: this.previous().value } as NumNode;
    }
    if (this.match("identifier")) {
      const name = this.previous().value;
      // Check if this is a function call like ceil(...)
      if (this.match("(")) {
        const args: Node[] = [];
        if (!this.check(")")) {
          do {
            args.push(this.boolExpr());
          } while (this.match(","));
        }
        this.consume(")", "expected ')'");
        if (!ALLOWED_FUNCTIONS.has(name)) {
          throw new ExpressionError(`disallowed function: ${name}`);
        }
        return { type: "call", name, args } as CallNode;
      }
      return { type: "identifier", name } as IdentNode;
    }
    // if(cond, then, else) — disambiguate from ternary by checking for '('
    if (this.match("if") && this.match("(")) {
      const args: Node[] = [];
      if (!this.check(")")) {
        do {
          args.push(this.boolExpr());
        } while (this.match(","));
      }
      this.consume(")", "expected ')'");
      return { type: "call", name: "if", args } as CallNode;
    }
    if (this.match("(")) {
      const expr = this.parse();
      this.consume(")", "expected ')'");
      return expr;
    }
    throw new ExpressionError(`unexpected token: '${this.peek()?.value ?? "EOF"}'`);
  }
}

// ── Helper arity / range validation (H6) ─────────────────────────────────────
// Static checks applied at parse/validate time so config-load (and the parity
// fixture's expect_error cases) reject malformed helper calls. Semantics must
// match the Python SDK exactly.

function checkCallArity(name: string, argc: number): void {
  switch (name) {
    case "if":
      if (argc !== 3)
        throw new ExpressionError("if() requires exactly 3 arguments: if(condition, then, else)");
      break;
    case "clamp":
      if (argc !== 3)
        throw new ExpressionError("clamp() requires exactly 3 arguments: clamp(x, min, max)");
      break;
    case "tier":
      // tier(value, t1, r1, [t2, r2, ...], default) — the value, then N>=1
      // (threshold, rate) pairs, then a trailing default. Arg count must be
      // EVEN and >= 4 (REFACTOR_CONTRACT.md §1): 4 args = 1 pair + default,
      // 6 args = 2 pairs + default, etc. Odd arg counts (3/5/7) and < 4 are
      // rejected. Semantics: return r_i for the first value < t_i, else default.
      if (argc < 4 || argc % 2 !== 0)
        throw new ExpressionError(
          "tier() requires an even number of arguments >= 4 (value, threshold, rate, ..., default)",
        );
      break;
    case "percentile":
      if (argc < 2)
        throw new ExpressionError(
          "percentile() requires at least 2 arguments (p, v1, [v2, ...])",
        );
      break;
    case "min":
      if (argc < 1) throw new ExpressionError("min() requires at least 1 argument");
      break;
    case "max":
      if (argc < 1) throw new ExpressionError("max() requires at least 1 argument");
      break;
    case "ceil":
    case "floor":
      if (argc !== 1) throw new ExpressionError(`${name}() requires exactly 1 argument`);
      break;
    case "round":
      if (argc !== 1 && argc !== 2)
        throw new ExpressionError("round() requires 1 or 2 arguments: round(x[, ndigits])");
      break;
    default:
      break;
  }
}

function validateCalls(node: Node): void {
  if (node.type === "call") {
    checkCallArity(node.name, node.args.length);
  }
  for (const child of children(node)) validateCalls(child);
}

// ── Validation ─────────────────────────────────────────────────────────────

function collectVariables(node: Node): Set<string> {
  const seen = new Set<string>();
  function walk(n: Node): void {
    if (n.type === "identifier" && !ALLOWED_FUNCTIONS.has(n.name)) {
      seen.add(n.name);
    }
    for (const child of children(n)) walk(child);
  }
  walk(node);
  return seen;
}

function children(n: Node): Node[] {
  switch (n.type) {
    case "binary":
      return [n.left, n.right];
    case "unary":
      return [n.operand];
    case "call":
      return n.args;
    case "ternary":
      return [n.cond, n.then, n.else];
    case "comparison":
      return [n.left, n.right];
    case "boolean":
      return [n.left, n.right];
    case "number":
    case "identifier":
      return [];
  }
}

// ── Public API ─────────────────────────────────────────────────────────────

/**
 * Validate that an expression string is safe and syntactically valid.
 *
 * @param expr The expression source.
 * @param knownVariables Optional set of metric variable names allowed in the
 *   expression. When provided (config-load passes the metric set from
 *   `PricingEngine.buildVariables`), any identifier not in this set and not an
 *   allowed function is rejected (M5) — typos fail at load, not at runtime.
 */
export function validateExpression(expr: string, knownVariables?: Iterable<string>): void {
  try {
    const tokens = tokenize(expr);
    const parser = new Parser(tokens);
    const node = parser.parse();
    if (!parser.isAtEnd()) {
      throw new ExpressionError(`unexpected token after expression: '${parser.peek()?.value}'`);
    }
    validateCalls(node);
    const vars = collectVariables(node);
    if (vars.size === 0) {
      throw new ExpressionError(
        "expression references no variables -- must use at least one metric",
      );
    }
    if (knownVariables !== undefined) {
      const known = knownVariables instanceof Set ? knownVariables : new Set(knownVariables);
      for (const name of vars) {
        if (!known.has(name)) {
          throw new ExpressionError(`unknown variable: '${name}'`);
        }
      }
    }
  } catch (e) {
    if (e instanceof ExpressionError) throw e;
    throw new ExpressionError(`invalid expression: ${(e as Error).message}`);
  }
}

/**
 * Safely evaluate a validated expression. Returns an exact `Decimal` — never a
 * binary-float `number` — so credit math is precise and parity-stable with the
 * Python `decimal.Decimal` engine.
 */
export function evaluateExpression(
  expr: string,
  variables: Record<string, number | Decimal>,
): Decimal {
  if (!variables || typeof variables !== "object") {
    throw new ExpressionError("variables must be a dict");
  }
  if (Object.keys(variables).length === 0) {
    throw new ExpressionError("cannot evaluate: variables dict is empty");
  }

  let tokens: Token[];
  try {
    tokens = tokenize(expr);
  } catch (e) {
    if (e instanceof ExpressionError) throw e;
    throw new ExpressionError(`syntax error: ${(e as Error).message}`);
  }

  const parser = new Parser(tokens);
  const node = parser.parse();
  if (!parser.isAtEnd()) {
    throw new ExpressionError(`unexpected token: '${parser.peek()?.value}'`);
  }

  // Arity/range validation (parity with Python, which validates on every eval).
  validateCalls(node);

  // Validate undefined variables using OWN-property checks only (C6). The `in`
  // operator walks the prototype chain, so identifiers like __proto__,
  // constructor, prototype, toString, hasOwnProperty would otherwise resolve to
  // inherited members. Object.prototype.hasOwnProperty.call rejects all of them.
  function checkVars(n: Node): void {
    if (
      n.type === "identifier" &&
      !ALLOWED_FUNCTIONS.has(n.name) &&
      !Object.prototype.hasOwnProperty.call(variables, n.name)
    ) {
      throw new ExpressionError(`undefined variable: '${n.name}'`);
    }
    for (const child of children(n)) checkVars(child);
  }
  checkVars(node);

  const result = evaluateNode(node, variables);

  // Guard non-finite results (C7): NaN/Infinity must never become a charge.
  if (!result.isFinite()) {
    throw new ExpressionError(`expression evaluated to a non-finite value: ${result.toString()}`);
  }
  return result;
}

const ZERO = new Decimal(0);
const ONE = new Decimal(1);

function toDecimal(v: number | Decimal): Decimal {
  return v instanceof Decimal ? v : new Decimal(v);
}

function truthy(d: Decimal): boolean {
  return !d.isZero();
}

function evaluateNode(node: Node, vars: Record<string, number | Decimal>): Decimal {
  switch (node.type) {
    case "number":
      // Parse from the exact literal string (never via binary float).
      return new Decimal(node.value);

    case "identifier": {
      // Own-property lookup only (C6); undefined identifiers were already
      // rejected by checkVars, but guard defensively here too.
      if (!Object.prototype.hasOwnProperty.call(vars, node.name)) {
        return ZERO;
      }
      return toDecimal(vars[node.name]);
    }

    case "unary": {
      const v = evaluateNode(node.operand, vars);
      if (node.op === "not") return truthy(v) ? ZERO : ONE;
      return node.op === "-" ? v.negated() : v;
    }

    case "binary": {
      const l = evaluateNode(node.left, vars);
      const r = evaluateNode(node.right, vars);
      switch (node.op) {
        case "+":
          return l.plus(r);
        case "-":
          return l.minus(r);
        case "*":
          return l.times(r);
        case "/":
          if (r.isZero()) throw new ExpressionError("division by zero");
          return l.dividedBy(r);
        case "//":
          if (r.isZero()) throw new ExpressionError("division by zero");
          // Floor division — matches Python's // and JS Math.floor(l / r).
          return l.dividedBy(r).floor();
        case "%":
          if (r.isZero()) throw new ExpressionError("modulo by zero");
          return l.modulo(r);
        default:
          throw new ExpressionError(`unknown operator: ${node.op}`);
      }
    }

    case "call": {
      const args = node.args.map((a) => evaluateNode(a, vars));
      switch (node.name) {
        case "ceil":
          return args[0].ceil();
        case "floor":
          return args[0].floor();
        case "min":
          if (args.length < 1) throw new ExpressionError("min() requires at least 1 argument");
          return Decimal.min(...args);
        case "max":
          if (args.length < 1) throw new ExpressionError("max() requires at least 1 argument");
          return Decimal.max(...args);
        case "round": {
          if (args.length !== 1 && args.length !== 2)
            throw new ExpressionError("round() requires 1 or 2 arguments: round(x[, ndigits])");
          const ndigits = args.length === 2 ? args[1].toNumber() : 0;
          return args[0].toDecimalPlaces(ndigits, Decimal.ROUND_HALF_UP);
        }
        case "if":
          if (args.length !== 3)
            throw new ExpressionError("if() requires exactly 3 arguments: if(condition, then, else)");
          return truthy(args[0]) ? args[1] : args[2];
        case "tier": {
          if (args.length < 4 || args.length % 2 !== 0)
            throw new ExpressionError(
              "tier() requires an even number of arguments >= 4 (value, threshold, rate, ..., default)",
            );
          const val = args[0];
          for (let i = 1; i < args.length - 1; i += 2) {
            if (val.lessThan(args[i])) return args[i + 1];
          }
          return args[args.length - 1]; // default
        }
        case "clamp":
          if (args.length !== 3)
            throw new ExpressionError("clamp() requires exactly 3 arguments: clamp(x, min, max)");
          return Decimal.max(args[1], Decimal.min(args[0], args[2]));
        case "percentile": {
          if (args.length < 2)
            throw new ExpressionError(
              "percentile() requires at least 2 arguments (p, v1, [v2, ...])",
            );
          const p = args[0];
          if (p.lessThan(0) || p.greaterThan(100))
            throw new ExpressionError("percentile() p must be between 0 and 100");
          const sorted = args.slice(1).sort((a, b) => a.comparedTo(b));
          const n = sorted.length;
          if (n === 1) return sorted[0];
          // rank = p/100 * (n - 1); linear interpolation between neighbours.
          const rank = p.dividedBy(100).times(n - 1);
          const lower = rank.floor();
          const lowerIdx = lower.toNumber();
          const upperIdx = Math.min(lowerIdx + 1, n - 1);
          const frac = rank.minus(lower);
          return sorted[lowerIdx].times(ONE.minus(frac)).plus(sorted[upperIdx].times(frac));
        }
        default:
          throw new ExpressionError(`disallowed function: ${node.name}`);
      }
    }

    case "ternary":
      return truthy(evaluateNode(node.cond, vars))
        ? evaluateNode(node.then, vars)
        : evaluateNode(node.else, vars);

    case "comparison": {
      const l = evaluateNode(node.left, vars);
      const r = evaluateNode(node.right, vars);
      switch (node.op) {
        case "==":
          return l.equals(r) ? ONE : ZERO;
        case "!=":
          return l.equals(r) ? ZERO : ONE;
        case "<":
          return l.lessThan(r) ? ONE : ZERO;
        case "<=":
          return l.lessThanOrEqualTo(r) ? ONE : ZERO;
        case ">":
          return l.greaterThan(r) ? ONE : ZERO;
        case ">=":
          return l.greaterThanOrEqualTo(r) ? ONE : ZERO;
        case "in":
          return l.toString().includes(r.toString()) ? ONE : ZERO;
        case "not in":
          return l.toString().includes(r.toString()) ? ZERO : ONE;
        default:
          throw new ExpressionError(`unknown comparison: ${node.op}`);
      }
    }

    case "boolean": {
      const l = evaluateNode(node.left, vars);
      switch (node.op) {
        case "and":
          // H4 fix: return operand (Python short-circuit semantics), not 0/1.
          // (5 and 7) → 7; (0 and 7) → 0; (5 and 0) → 0.
          if (!truthy(l)) return l;
          return evaluateNode(node.right, vars);
        case "or":
          // (5 or 7) → 5; (0 or 7) → 7; (0 or 0) → 0.
          if (truthy(l)) return l;
          return evaluateNode(node.right, vars);
        default:
          throw new ExpressionError(`unknown boolean op: ${node.op}`);
      }
    }
  }
}
