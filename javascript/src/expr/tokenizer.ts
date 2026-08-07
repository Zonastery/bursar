import { ExpressionError } from "../errors.js";

export type TokenType =
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

export interface Token {
  type: TokenType;
  value: string;
}

const TWO_CHARACTER_OPERATORS = new Set<TokenType>(["**", "//", "==", "!=", "<=", ">="]);
const ONE_CHARACTER_TOKENS = new Set<TokenType>(["(", ")", ",", "+", "-", "*", "/", "%", "<", ">"]);
const KEYWORDS = new Set<TokenType>(["and", "or", "if", "else", "in", "not"]);

function readNumber(source: string, start: number): { value: string; next: number } {
  let index = start;
  let value = "";
  let dotSeen = false;
  while (index < source.length && /[0-9.]/.test(source.charAt(index))) {
    const character = source.charAt(index);
    if (character === ".") {
      if (dotSeen) throw new ExpressionError(`invalid number literal: '${value}.'`);
      dotSeen = true;
    }
    value += character;
    index++;
  }
  if (value === "." || !/^[0-9]*\.?[0-9]*$/.test(value) || !/[0-9]/.test(value)) {
    throw new ExpressionError(`invalid number literal: '${value}'`);
  }

  if (index < source.length && /[eE]/.test(source.charAt(index))) {
    value += source.charAt(index++);
    if (index < source.length && /[+-]/.test(source.charAt(index))) {
      value += source.charAt(index++);
    }
    if (index >= source.length || !/[0-9]/.test(source.charAt(index))) {
      throw new ExpressionError(`invalid number literal: '${value}'`);
    }
    while (index < source.length && /[0-9]/.test(source.charAt(index))) {
      value += source.charAt(index++);
    }
  }
  return { value, next: index };
}

function readWord(source: string, start: number): { value: string; next: number } {
  let index = start;
  let value = "";
  while (index < source.length && /[a-zA-Z0-9_]/.test(source.charAt(index))) {
    value += source.charAt(index++);
  }
  return { value, next: index };
}

export function tokenize(source: string): Token[] {
  const tokens: Token[] = [];
  let index = 0;
  while (index < source.length) {
    const character = source.charAt(index);
    if (/\s/.test(character)) {
      index++;
      continue;
    }

    const pair = source.slice(index, index + 2) as TokenType;
    if (TWO_CHARACTER_OPERATORS.has(pair)) {
      tokens.push({ type: pair, value: pair });
      index += 2;
      continue;
    }

    const tokenType = character as TokenType;
    if (ONE_CHARACTER_TOKENS.has(tokenType)) {
      tokens.push({ type: tokenType, value: character });
      index++;
      continue;
    }

    if (/[0-9.]/.test(character)) {
      const number = readNumber(source, index);
      tokens.push({ type: "number", value: number.value });
      index = number.next;
      continue;
    }

    if (/[a-zA-Z_]/.test(character)) {
      const word = readWord(source, index);
      const isBoolean = word.value === "true" || word.value === "false";
      tokens.push({
        type: isBoolean
          ? "number"
          : KEYWORDS.has(word.value as TokenType)
            ? (word.value as TokenType)
            : "identifier",
        value: word.value === "true" ? "1" : word.value === "false" ? "0" : word.value,
      });
      index = word.next;
      continue;
    }

    throw new ExpressionError(`unexpected character: '${character}'`);
  }
  return tokens;
}
