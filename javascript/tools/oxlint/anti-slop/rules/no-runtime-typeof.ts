import { defineRule } from "@oxlint/plugins";

import type { ESTree } from "@oxlint/plugins";

type RuntimeFunction = ESTree.ArrowFunctionExpression | ESTree.Function;

function isRuntimeFunction(node: ESTree.Node): node is RuntimeFunction {
  return (
    node.type === "ArrowFunctionExpression" ||
    node.type === "FunctionDeclaration" ||
    node.type === "FunctionExpression"
  );
}

function isInsideTypeGuard(node: ESTree.Node): boolean {
  let current: ESTree.Node | null = node.parent;
  while (current !== null && current.type !== "Program") {
    if (isRuntimeFunction(current)) {
      return current.returnType?.typeAnnotation.type === "TSTypePredicate";
    }
    current = current.parent;
  }
  return false;
}

function unwrapParenthesized(node: ESTree.Expression): ESTree.Expression {
  let current = node;
  while (current.type === "ParenthesizedExpression") {
    current = current.expression;
  }
  return current;
}

function isObjectTagLiteral(node: ESTree.Expression): node is ESTree.StringLiteral {
  return node.type === "Literal" && node.value === "object";
}

function isObjectTypeof(node: ESTree.Expression): node is ESTree.UnaryExpression {
  const expression = unwrapParenthesized(node);
  return expression.type === "UnaryExpression" && expression.operator === "typeof";
}

function isObjectTypeofComparison(node: ESTree.BinaryExpression): boolean {
  if (
    node.operator !== "==" &&
    node.operator !== "!=" &&
    node.operator !== "===" &&
    node.operator !== "!=="
  ) {
    return false;
  }

  return (
    (isObjectTypeof(node.left) && isObjectTagLiteral(unwrapParenthesized(node.right))) ||
    (isObjectTagLiteral(unwrapParenthesized(node.left)) && isObjectTypeof(node.right))
  );
}

/** Disallow broad `typeof ... === "object"` checks that do not establish a contract. */
export const noRuntimeTypeofRule = defineRule({
  meta: {
    type: "problem",
    docs: {
      description:
        "Disallow broad typeof-object checks; decode external values into meaningful types at their I/O boundary.",
    },
    messages: {
      runtimeTypeof:
        'Avoid a broad `typeof ... === "object"` contract check. Parse the value at its I/O boundary, then branch on the domain type.',
    },
    schema: [
      {
        type: "object",
        properties: {
          allowInTypeGuards: {
            type: "boolean",
            description:
              "Allow typeof-object checks inside functions with an explicit TypeScript type-predicate return type.",
          },
        },
        additionalProperties: false,
      },
    ],
    defaultOptions: [{ allowInTypeGuards: false }],
  },
  createOnce(context) {
    return {
      BinaryExpression(node) {
        const option = context.options?.[0];
        const allowInTypeGuards =
          typeof option === "object" &&
          option !== null &&
          !Array.isArray(option) &&
          option.allowInTypeGuards === true;
        if (isObjectTypeofComparison(node) && (!allowInTypeGuards || !isInsideTypeGuard(node))) {
          const typeofExpression = isObjectTypeof(node.left)
            ? unwrapParenthesized(node.left)
            : unwrapParenthesized(node.right);
          context.report({ node: typeofExpression, messageId: "runtimeTypeof" });
        }
      },
    };
  },
});
