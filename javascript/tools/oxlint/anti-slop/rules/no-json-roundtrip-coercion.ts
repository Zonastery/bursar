import { defineRule } from "@oxlint/plugins";

import type { ESTree, Scope, SourceCode, Variable } from "@oxlint/plugins";

function unwrapSafeExpression(expression: ESTree.Expression): ESTree.Expression {
  let current = expression;
  while (current.type === "ParenthesizedExpression" || current.type === "ChainExpression") {
    current = current.expression;
  }
  return current;
}

function memberName(expression: ESTree.Expression): string | null {
  const unwrapped = unwrapSafeExpression(expression);
  if (unwrapped.type !== "MemberExpression") return null;
  if (!unwrapped.computed) {
    return unwrapped.property.type === "Identifier" ? unwrapped.property.name : null;
  }
  return unwrapped.property.type === "Literal" && typeof unwrapped.property.value === "string"
    ? unwrapped.property.value
    : null;
}

function isGlobalJson(sourceCode: SourceCode, expression: ESTree.Expression): boolean {
  const unwrapped = unwrapSafeExpression(expression);
  if (
    unwrapped.type === "Identifier" &&
    unwrapped.name === "JSON" &&
    sourceCode.isGlobalReference(unwrapped)
  ) {
    return true;
  }
  return (
    unwrapped.type === "MemberExpression" &&
    memberName(unwrapped) === "JSON" &&
    isGlobalThis(sourceCode, unwrapped.object)
  );
}

function isGlobalThis(sourceCode: SourceCode, expression: ESTree.Expression): boolean {
  const unwrapped = unwrapSafeExpression(expression);
  return (
    unwrapped.type === "Identifier" &&
    unwrapped.name === "globalThis" &&
    sourceCode.isGlobalReference(unwrapped)
  );
}

function isGlobalJsonMethodCall(
  sourceCode: SourceCode,
  expression: ESTree.Expression,
  methodName: string,
): boolean {
  const unwrapped = unwrapSafeExpression(expression);
  return (
    unwrapped.type === "MemberExpression" &&
    memberName(unwrapped) === methodName &&
    isGlobalJson(sourceCode, unwrapped.object)
  );
}

function resolveVariable(
  sourceCode: SourceCode,
  identifier: ESTree.IdentifierReference,
): Variable | null {
  let scope: Scope | null = sourceCode.getScope(identifier);
  while (scope !== null) {
    const variable = scope.set.get(identifier.name);
    if (variable !== undefined) return variable;
    scope = scope.upper;
  }
  return null;
}

function stableConstInitializer(variable: Variable): ESTree.Expression | null {
  if (variable.defs.length !== 1) return null;
  const [definition] = variable.defs;
  if (definition?.type !== "Variable" || definition.node.type !== "VariableDeclarator") {
    return null;
  }
  const declarator = definition.node;
  if (
    declarator.parent.type !== "VariableDeclaration" ||
    declarator.parent.kind !== "const" ||
    declarator.id.type !== "Identifier" ||
    declarator.init === null ||
    variable.references.some((reference) => reference.isWrite() && !reference.init)
  ) {
    return null;
  }
  return declarator.init;
}

function isJsonStringifySource(
  sourceCode: SourceCode,
  expression: ESTree.Expression,
  visited: ReadonlySet<Variable> = new Set(),
): boolean {
  const unwrapped = unwrapSafeExpression(expression);
  if (
    unwrapped.type === "CallExpression" &&
    unwrapped.callee.type !== "Super" &&
    unwrapped.callee.type !== "V8IntrinsicExpression"
  ) {
    return isGlobalJsonMethodCall(sourceCode, unwrapped.callee, "stringify");
  }
  if (unwrapped.type !== "Identifier") return false;

  const variable = resolveVariable(sourceCode, unwrapped);
  if (variable === null || visited.has(variable)) return false;
  const initializer = stableConstInitializer(variable);
  if (initializer === null) return false;
  return isJsonStringifySource(sourceCode, initializer, new Set([...visited, variable]));
}

/** Ban lossy JSON round trips used for coercion or deep cloning. */
export const noJsonRoundtripCoercionRule = defineRule({
  meta: {
    type: "problem",
    docs: {
      description:
        "Disallow global JSON.parse(JSON.stringify(value)) round trips used for coercion or deep cloning.",
    },
    messages: {
      jsonRoundtrip:
        "Do not use a JSON round trip to coerce a value. Validate and project boundary input into the target type, or use structuredClone when you need a deep clone.",
    },
  },
  createOnce(context) {
    return {
      CallExpression(node) {
        if (node.callee.type === "Super" || node.callee.type === "V8IntrinsicExpression") return;
        if (!isGlobalJsonMethodCall(context.sourceCode, node.callee, "parse")) return;

        const [serialized] = node.arguments;
        if (serialized === undefined || serialized.type === "SpreadElement") return;
        if (isJsonStringifySource(context.sourceCode, serialized)) {
          context.report({ node, messageId: "jsonRoundtrip" });
        }
      },
    };
  },
});
