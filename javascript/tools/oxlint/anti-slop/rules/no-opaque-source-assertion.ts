import { defineRule } from "@oxlint/plugins";
import type { ESTree, Scope, SourceCode, Variable } from "@oxlint/plugins";

type TypeAssertion = ESTree.TSAsExpression | ESTree.TSTypeAssertion;
type YamlModule = "yaml" | "js-yaml";

type ImportedBinding = {
  readonly module: YamlModule;
  readonly kind: "parser" | "namespace";
};

const yamlParserNames: Readonly<Record<YamlModule, ReadonlySet<string>>> = {
  yaml: new Set(["parse", "parseAll", "parseAllDocuments", "parseDocument"]),
  "js-yaml": new Set(["load", "loadAll", "safeLoad", "safeLoadAll"]),
};

function resolveVariable(
  sourceCode: SourceCode,
  identifier: ESTree.Node & { readonly name: string },
): Variable | null {
  let scope: Scope | null = sourceCode.getScope(identifier);
  while (scope !== null) {
    const variable = scope.set.get(identifier.name);
    if (variable !== undefined) return variable;
    scope = scope.upper;
  }
  return null;
}

function unwrapSourceWrappers(expression: ESTree.Expression): ESTree.Expression {
  let current = expression;
  while (true) {
    if (current.type === "AwaitExpression") {
      current = current.argument;
    } else if (
      current.type === "ChainExpression" ||
      current.type === "ParenthesizedExpression" ||
      current.type === "TSNonNullExpression" ||
      current.type === "TSSatisfiesExpression"
    ) {
      current = current.expression;
    } else {
      return current;
    }
  }
}

function unwrapAliasWrappers(expression: ESTree.Expression): ESTree.Expression {
  let current = expression;
  while (true) {
    if (current.type === "AwaitExpression") {
      current = current.argument;
    } else if (
      current.type === "ChainExpression" ||
      current.type === "ParenthesizedExpression" ||
      current.type === "TSAsExpression" ||
      current.type === "TSNonNullExpression" ||
      current.type === "TSSatisfiesExpression" ||
      current.type === "TSTypeAssertion"
    ) {
      current = current.expression;
    } else {
      return current;
    }
  }
}

function propertyName(member: ESTree.MemberExpression): string | null {
  if (!member.computed && member.property.type === "Identifier") return member.property.name;
  return member.computed &&
    member.property.type === "Literal" &&
    typeof member.property.value === "string"
    ? member.property.value
    : null;
}

function isGlobalJsonIdentifier(
  sourceCode: SourceCode,
  identifier: ESTree.IdentifierReference,
): boolean {
  if (sourceCode.isGlobalReference(identifier)) return true;
  const variable = resolveVariable(sourceCode, identifier);
  return variable === null || variable.defs.length === 0;
}

function isGlobalThis(sourceCode: SourceCode, expression: ESTree.Expression): boolean {
  return (
    expression.type === "Identifier" &&
    expression.name === "globalThis" &&
    sourceCode.isGlobalReference(expression)
  );
}

function isGlobalJsonObject(sourceCode: SourceCode, expression: ESTree.Expression): boolean {
  if (expression.type === "Identifier" && expression.name === "JSON") {
    return isGlobalJsonIdentifier(sourceCode, expression);
  }
  return (
    expression.type === "MemberExpression" &&
    propertyName(expression) === "JSON" &&
    isGlobalThis(sourceCode, expression.object)
  );
}

function isJsonParseCall(sourceCode: SourceCode, call: ESTree.CallExpression): boolean {
  const { callee } = call;
  if (callee.type !== "MemberExpression" || propertyName(callee) !== "parse") return false;
  return isGlobalJsonObject(sourceCode, callee.object);
}

function isResponseJsonCall(call: ESTree.CallExpression): boolean {
  return call.callee.type === "MemberExpression" && propertyName(call.callee) === "json";
}

function importName(specifier: ESTree.ImportSpecifier): string | null {
  const imported = specifier.imported;
  if (imported.type === "Identifier") return imported.name;
  return typeof imported.value === "string" ? imported.value : null;
}

function collectYamlImports(
  sourceCode: SourceCode,
  program: ESTree.Program,
): ReadonlyMap<Variable, ImportedBinding> {
  const bindings = new Map<Variable, ImportedBinding>();

  for (const statement of program.body) {
    if (statement.type !== "ImportDeclaration" || typeof statement.source.value !== "string")
      continue;
    const module = statement.source.value;
    if (module !== "yaml" && module !== "js-yaml") continue;

    for (const specifier of statement.specifiers) {
      let binding: ImportedBinding;
      if (specifier.type === "ImportSpecifier") {
        const importedName = importName(specifier);
        if (importedName === null || !yamlParserNames[module].has(importedName)) continue;
        binding = { module, kind: "parser" };
      } else {
        binding = { module, kind: "namespace" };
      }

      const variable = resolveVariable(sourceCode, specifier.local);
      if (variable !== null) bindings.set(variable, binding);
    }
  }

  return bindings;
}

function isYamlParserCall(
  sourceCode: SourceCode,
  call: ESTree.CallExpression,
  imports: ReadonlyMap<Variable, ImportedBinding>,
): boolean {
  if (call.callee.type === "Identifier") {
    const variable = resolveVariable(sourceCode, call.callee);
    const binding = variable === null ? undefined : imports.get(variable);
    return binding?.kind === "parser";
  }

  if (call.callee.type !== "MemberExpression") return false;
  const parserName = propertyName(call.callee);
  if (parserName === null || call.callee.object.type !== "Identifier") return false;
  const variable = resolveVariable(sourceCode, call.callee.object);
  const binding = variable === null ? undefined : imports.get(variable);
  return (
    binding !== undefined &&
    binding.kind === "namespace" &&
    yamlParserNames[binding.module].has(parserName)
  );
}

function variableDeclarator(variable: Variable): ESTree.VariableDeclarator | null {
  for (const definition of variable.defs) {
    if (definition.type === "Variable" && definition.node.type === "VariableDeclarator") {
      return definition.node;
    }
  }
  return null;
}

function isStableConstAlias(variable: Variable): ESTree.VariableDeclarator | null {
  const declarator = variableDeclarator(variable);
  if (
    declarator === null ||
    declarator.parent.type !== "VariableDeclaration" ||
    declarator.parent.kind !== "const" ||
    declarator.id.type !== "Identifier" ||
    declarator.init === null ||
    variable.references.some((reference) => reference.isWrite() && !reference.init)
  ) {
    return null;
  }
  return declarator;
}

function isOpaqueSource(
  sourceCode: SourceCode,
  expression: ESTree.Expression,
  imports: ReadonlyMap<Variable, ImportedBinding>,
  visited: ReadonlySet<Variable> = new Set(),
): boolean {
  const current = unwrapSourceWrappers(expression);

  if (current.type === "CallExpression") {
    return (
      isJsonParseCall(sourceCode, current) ||
      isResponseJsonCall(current) ||
      isYamlParserCall(sourceCode, current, imports)
    );
  }

  if (current.type !== "Identifier") return false;
  const variable = resolveVariable(sourceCode, current);
  if (variable === null || visited.has(variable)) return false;
  const declarator = isStableConstAlias(variable);
  if (declarator === null) return false;

  const nextVisited = new Set(visited);
  nextVisited.add(variable);
  if (declarator.init === null) return false;
  const initializer = unwrapAliasWrappers(declarator.init);
  return isOpaqueSource(sourceCode, initializer, imports, nextVisited);
}

/** Require schema/parser validation before asserting a type onto opaque parsed data. */
export const noOpaqueSourceAssertionRule = defineRule({
  meta: {
    type: "problem",
    docs: {
      description:
        "Disallow TypeScript assertions directly on JSON, response JSON, and YAML parser results, including stable const aliases.",
    },
    messages: {
      opaqueSource:
        "This type assertion trusts opaque parsed data. Require schema/parser validation before narrowing it.",
    },
  },
  createOnce(context) {
    let yamlImports: ReadonlyMap<Variable, ImportedBinding> = new Map();

    const checkAssertion = (node: TypeAssertion) => {
      if (node.typeAnnotation.type === "TSUnknownKeyword") return;
      if (isOpaqueSource(context.sourceCode, node.expression, yamlImports)) {
        context.report({ node, messageId: "opaqueSource" });
      }
    };

    return {
      Program(node) {
        yamlImports = collectYamlImports(context.sourceCode, node);
      },
      TSAsExpression: checkAssertion,
      TSTypeAssertion: checkAssertion,
    };
  },
});
