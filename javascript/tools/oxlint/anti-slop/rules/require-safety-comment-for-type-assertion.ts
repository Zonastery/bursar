import { defineRule } from "@oxlint/plugins";

import type { ESTree, SourceCode } from "@oxlint/plugins";

type TypeAssertion = ESTree.TSAsExpression | ESTree.TSTypeAssertion;

const commentOwnerKinds = new Set([
  "ExpressionStatement",
  "PropertyDefinition",
  "ReturnStatement",
  "ThrowStatement",
  "VariableDeclaration",
]);

function isConstAssertion(node: TypeAssertion): boolean {
  return (
    node.typeAnnotation.type === "TSTypeReference" &&
    node.typeAnnotation.typeName.type === "Identifier" &&
    node.typeAnnotation.typeName.name === "const"
  );
}

function hasConcreteSafetyComment(sourceCode: SourceCode, node: TypeAssertion): boolean {
  let current: ESTree.Node = node;
  while (true) {
    if (
      sourceCode.getCommentsBefore(current).some((comment) => {
        if (comment.end > node.start) return false;
        const match = /\bSAFETY\s*:\s*([\s\S]*)/u.exec(comment.value);
        return match !== null && match[1].trim().length > 0;
      })
    ) {
      return true;
    }
    if (commentOwnerKinds.has(current.type) || current.parent.type === "Program") return false;
    current = current.parent;
  }
}

/** Require every non-const type assertion to state the invariant TypeScript cannot express. */
export const requireSafetyCommentForTypeAssertionRule = defineRule({
  meta: {
    type: "problem",
    docs: {
      description:
        "Require a nearby non-empty SAFETY justification for every TypeScript type assertion except const assertions; comments document invariants but never override hard assertion rules.",
    },
    messages: {
      missingSafetyComment:
        "This type assertion has no non-empty `SAFETY:` justification. State the concrete checked invariant immediately before the assertion or its containing statement; a comment never overrides hard assertion rules.",
    },
  },
  createOnce(context) {
    const checkAssertion = (node: TypeAssertion) => {
      if (isConstAssertion(node) || hasConcreteSafetyComment(context.sourceCode, node)) return;
      context.report({ node, messageId: "missingSafetyComment" });
    };

    return {
      TSAsExpression: checkAssertion,
      TSTypeAssertion: checkAssertion,
    };
  },
});
