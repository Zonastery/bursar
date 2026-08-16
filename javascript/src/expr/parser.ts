import { ExpressionError } from "../errors.js";
import type { Node } from "./ast.js";
import { ALLOWED_FUNCTIONS } from "./language.js";
import { tokenize, type Token, type TokenType } from "./tokenizer.js";

export class ExpressionParser {
  private position = 0;

  constructor(private readonly tokens: Token[]) {}

  static fromSource(source: string): ExpressionParser {
    return new ExpressionParser(tokenize(source));
  }

  peek(): Token | undefined {
    return this.tokens[this.position];
  }

  isAtEnd(): boolean {
    return this.position >= this.tokens.length;
  }

  private previous(): Token {
    const token = this.tokens[this.position - 1];
    if (!token) throw new ExpressionError("invalid parser state: no previous token");
    return token;
  }

  private check(...types: TokenType[]): boolean {
    const token = this.peek();
    return token !== undefined && types.includes(token.type);
  }

  private match(...types: TokenType[]): boolean {
    for (const type of types) {
      if (this.check(type)) {
        this.position++;
        return true;
      }
    }
    return false;
  }

  private consume(type: TokenType, message: string): Token {
    const token = this.peek();
    if (token?.type === type) {
      this.position++;
      return token;
    }
    throw new ExpressionError(message);
  }

  parse(): Node {
    const expression = this.booleanExpression();
    if (!this.match("if")) return expression;

    const condition = this.booleanExpression();
    this.consume("else", "expected 'else' in ternary expression");
    return {
      type: "ternary",
      cond: condition,
      then: expression,
      else: this.booleanExpression(),
    };
  }

  private comparison(): Node {
    const left = this.addition();
    let node: Node;
    if (this.check("not")) {
      this.match("not");
      if (!this.match("in")) {
        throw new ExpressionError("expected 'in' after 'not'");
      }
      node = { type: "comparison", op: "not in", left, right: this.addition() };
    } else if (this.match("==", "!=", "<", "<=", ">", ">=", "in")) {
      node = {
        type: "comparison",
        op: this.previous().value,
        left,
        right: this.addition(),
      };
    } else {
      return left;
    }
    // Reject chained comparisons (a < b < c): a left-associative fold would
    // evaluate them as ((a<b)<c) whereas Python uses and-chaining semantics, so
    // forbidding them keeps the two engines identical (parity).
    if (this.check("==", "!=", "<", "<=", ">", ">=", "in", "not")) {
      throw new ExpressionError("chained comparisons are not supported");
    }
    return node;
  }

  private addition(): Node {
    let left = this.multiplication();
    while (this.match("+", "-")) {
      left = {
        type: "binary",
        op: this.previous().value,
        left,
        right: this.multiplication(),
      };
    }
    return left;
  }

  private multiplication(): Node {
    let left = this.unary();
    while (this.match("*", "/", "//", "%")) {
      left = {
        type: "binary",
        op: this.previous().value,
        left,
        right: this.unary(),
      };
    }
    if (this.check("**")) {
      throw new ExpressionError(
        "exponentiation operator '**' is not allowed in pricing expressions",
      );
    }
    return left;
  }

  private notExpression(): Node {
    if (!this.match("not")) return this.comparison();
    return {
      type: "unary",
      op: "not",
      operand: this.notExpression(),
    };
  }

  private andExpression(): Node {
    let left = this.notExpression();
    while (this.match("and")) {
      left = {
        type: "boolean",
        op: "and",
        left,
        right: this.notExpression(),
      };
    }
    return left;
  }

  private booleanExpression(): Node {
    let left = this.andExpression();
    while (this.match("or")) {
      left = {
        type: "boolean",
        op: "or",
        left,
        right: this.andExpression(),
      };
    }
    return left;
  }

  private unary(): Node {
    if (!this.match("+", "-")) return this.primary();
    return {
      type: "unary",
      op: this.previous().value,
      operand: this.unary(),
    };
  }

  private primary(): Node {
    if (this.check("**")) {
      throw new ExpressionError(
        "exponentiation operator '**' is not allowed in pricing expressions",
      );
    }
    if (this.match("number")) {
      return { type: "number", value: this.previous().value };
    }
    if (this.match("identifier")) {
      const name = this.previous().value;
      if (!this.match("(")) return { type: "identifier", name };
      if (!ALLOWED_FUNCTIONS.has(name)) {
        throw new ExpressionError(`disallowed function: ${name}`);
      }
      return { type: "call", name, args: this.callArguments() };
    }
    if (this.match("if") && this.match("(")) {
      return { type: "call", name: "if", args: this.callArguments() };
    }
    if (this.match("(")) {
      const expression = this.parse();
      this.consume(")", "expected ')'");
      return expression;
    }
    throw new ExpressionError(`unexpected token: '${this.peek()?.value ?? "EOF"}'`);
  }

  private callArguments(): Node[] {
    const args: Node[] = [];
    if (!this.check(")")) {
      do {
        args.push(this.booleanExpression());
      } while (this.match(","));
    }
    this.consume(")", "expected ')'");
    return args;
  }
}
