import { ExpressionError } from "../errors.js";
import type {
  BinOpNode,
  BoolOpNode,
  CallNode,
  CompareNode,
  IdentNode,
  Node,
  NumNode,
  TernaryNode,
  UnaryNode,
} from "./ast.js";
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
    return this.tokens[this.position - 1];
  }

  private check(...types: TokenType[]): boolean {
    return !this.isAtEnd() && types.includes(this.tokens[this.position].type);
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
    if (this.check(type)) return this.tokens[this.position++];
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
    } as TernaryNode;
  }

  private comparison(): Node {
    let left = this.addition();
    if (this.match("not") && this.match("in")) {
      left = {
        type: "comparison",
        op: "not in",
        left,
        right: this.addition(),
      } as CompareNode;
    }
    while (this.match("==", "!=", "<", "<=", ">", ">=", "in")) {
      left = {
        type: "comparison",
        op: this.previous().value,
        left,
        right: this.addition(),
      } as CompareNode;
    }
    return left;
  }

  private addition(): Node {
    let left = this.multiplication();
    while (this.match("+", "-")) {
      left = {
        type: "binary",
        op: this.previous().value,
        left,
        right: this.multiplication(),
      } as BinOpNode;
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
      } as BinOpNode;
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
    } as UnaryNode;
  }

  private booleanExpression(): Node {
    let left = this.notExpression();
    while (this.match("and", "or")) {
      left = {
        type: "boolean",
        op: this.previous().value,
        left,
        right: this.notExpression(),
      } as BoolOpNode;
    }
    return left;
  }

  private unary(): Node {
    if (!this.match("+", "-")) return this.primary();
    return {
      type: "unary",
      op: this.previous().value,
      operand: this.unary(),
    } as UnaryNode;
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
      if (!this.match("(")) return { type: "identifier", name } as IdentNode;
      if (!ALLOWED_FUNCTIONS.has(name)) {
        throw new ExpressionError(`disallowed function: ${name}`);
      }
      return { type: "call", name, args: this.callArguments() } as CallNode;
    }
    if (this.match("if") && this.match("(")) {
      return { type: "call", name: "if", args: this.callArguments() } as CallNode;
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
