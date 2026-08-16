export const fixtures = [
  {
    rule: "no-chained-type-assertions",
    valid: ["const value = { id: 1 } as const;"],
    invalid: ["const value = input as unknown as { id: number };"],
  },
  {
    rule: "no-conditional-empty-object-spread",
    valid: ["const value = { ...details, enabled: true };"],
    invalid: ["const value = { ...(enabled ? { enabled } : {}) };"],
  },
  {
    rule: "no-json-roundtrip-coercion",
    valid: [
      "const copy = structuredClone(value);",
      "const parsed = JSON.parse(text);",
      "const encoded = JSON.stringify(value);",
      "const JSON = { parse: (value) => value, stringify: (value) => value }; const copy = JSON.parse(JSON.stringify(value));",
    ],
    invalid: [
      "const copy = JSON.parse(JSON.stringify(value));",
      "const copy = globalThis.JSON.parse(JSON.stringify(value));",
      "const encoded = JSON.stringify(value); const copy = JSON.parse(encoded);",
    ],
  },
  {
    rule: "no-known-value-widening",
    valid: ["const value = { id: 1 };"],
    invalid: ["const value: object = { id: 1 };"],
  },
  {
    rule: "no-module-mocking",
    valid: ["const vi = { mock() {} }; vi.mock();"],
    invalid: [
      'import { vi } from "vitest"; vi.mock("./service");',
      'import { jest } from "@jest/globals"; jest.unstable_mockModule("./service", () => ({}));',
    ],
  },
  {
    rule: "no-object-parameters",
    valid: ["interface RequestData { id: string } function read(value: RequestData) {}"],
    invalid: ["function read(value: object) {}"],
  },
  {
    rule: "no-opaque-source-assertion",
    valid: [
      "const value = DomainSchema.parse(JSON.parse(text));",
      "const value = { id: 1 } as DomainValue;",
      "const value = JSON.parse(text) as unknown;",
      "const JSON = { parse: (value) => value }; const value = JSON.parse(text) as DomainValue;",
      'import { stringify } from "yaml"; const value = stringify(document) as string;',
    ],
    invalid: [
      "const value = JSON.parse(text) as DomainValue;",
      "const value = globalThis.JSON.parse(text) as DomainValue;",
      "const parsed = JSON.parse(text); const value = parsed as DomainValue;",
      "async function read(response: Response) { return (await response.json()) as DomainValue; }",
      "async function read(response: Response) { const parsed = await response.json(); return parsed as DomainValue; }",
      'import { parse } from "yaml"; const parsed = parse(text); const value = parsed as DomainValue;',
      'import yaml from "js-yaml"; const value = yaml.load(text) as DomainValue;',
      'import * as yaml from "yaml"; const value = <DomainValue>yaml.parse(text);',
    ],
  },
  {
    rule: "no-reflect-apply",
    valid: ["const Reflect = { apply() {} }; Reflect.apply();"],
    invalid: ["Reflect.apply(callback, undefined, []);"],
  },
  {
    rule: "no-reflect-get",
    valid: ["const Reflect = { get() {} }; Reflect.get();"],
    invalid: ["const value = Reflect.get(record, key);"],
  },
  {
    rule: "no-runtime-typeof",
    valid: [
      'const valid = typeof value === "string";',
      'const valid = typeof value === "number";',
      'const valid = typeof value === "bigint";',
      'const valid = typeof value === "boolean";',
      'const valid = typeof value === "symbol";',
      'const valid = typeof value === "function";',
      'const valid = typeof value === "undefined";',
      "const tag = typeof value;",
    ],
    invalid: [
      'const valid = typeof value === "object";',
      'const valid = "object" !== typeof value;',
    ],
  },
  {
    rule: "no-shape-in-symbol-names",
    valid: ["const payloadStructure = {};"],
    invalid: ["const payloadShape = {};"],
  },
  {
    rule: "no-unknown-parameters",
    valid: [
      "function fromCause(cause: unknown) {}",
      "function fromError(error: unknown) {}",
      "function fromErr(err: unknown) {}",
      "function fromReason(reason: unknown) {}",
    ],
    invalid: ["function decode(payload: unknown) {}"],
  },
  {
    rule: "no-unknown-returns",
    valid: ['function decode(): string { return "value"; }'],
    invalid: ["function decode(): unknown { return value; }"],
  },
  {
    rule: "no-unknown-type-aliases",
    valid: ["type Failure = { cause: unknown };"],
    invalid: ["type ParsedValue = unknown;"],
  },
  {
    rule: "no-unsafe-dictionary-type",
    valid: ["type Labels = Record<string, string>;"],
    invalid: ["type Labels = Record<string, unknown>;"],
  },
  {
    rule: "no-widen-then-assert",
    valid: ["const value = { id: 1 }; const id = value.id;"],
    invalid: ["const erased: object = { id: 1 }; const restored = erased as { id: number };"],
  },
  {
    rule: "require-safety-comment-for-type-assertion",
    valid: [
      "// SAFETY: DomainSchema validated this value immediately above.\nconst parsed = value as DomainValue;",
      "const literal = { id: 1 } as const;",
    ],
    invalid: [
      "const parsed = value as DomainValue;",
      "// SAFETY:\nconst parsed = value as DomainValue;",
    ],
  },
];
