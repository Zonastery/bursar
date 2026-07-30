import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { extname, join, relative } from "node:path";
import ts from "typescript";

const sourceRoot = new URL("../src/", import.meta.url);

function sourceFiles(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) {
      return entry.name === "generated" ? [] : sourceFiles(path);
    }
    return extname(entry.name) === ".ts" ? [path] : [];
  });
}

function hasModifier(node, kind) {
  return node.modifiers?.some((modifier) => modifier.kind === kind) ?? false;
}

function memberName(member) {
  const name = member.name;
  if (!name) return null;
  if (ts.isIdentifier(name) || ts.isPrivateIdentifier(name)) return name.text;
  if (ts.isStringLiteral(name) || ts.isNumericLiteral(name)) return name.text;
  return name.getText();
}

function parameters(node) {
  return (node.parameters ?? []).map((parameter) => ({
    name: parameter.name.getText(),
    optional: Boolean(parameter.questionToken || parameter.initializer),
    rest: Boolean(parameter.dotDotDotToken),
  }));
}

function membersOf(node) {
  const members =
    ts.isTypeAliasDeclaration(node) && ts.isTypeLiteralNode(node.type)
      ? node.type.members
      : (node.members ?? []);
  return members.flatMap((member) => {
    const name = memberName(member);
    if (!name || name.startsWith("#")) return [];
    const visibility = hasModifier(member, ts.SyntaxKind.PrivateKeyword)
      ? "private"
      : hasModifier(member, ts.SyntaxKind.ProtectedKeyword)
        ? "protected"
        : "public";
    let kind = "property";
    if (
      ts.isMethodDeclaration(member) ||
      ts.isMethodSignature(member) ||
      ts.isCallSignatureDeclaration(member)
    ) {
      kind = "method";
    } else if (ts.isGetAccessorDeclaration(member)) {
      kind = "getter";
    } else if (ts.isSetAccessorDeclaration(member)) {
      kind = "setter";
    }
    return [
      {
        name,
        kind,
        visibility,
        static: hasModifier(member, ts.SyntaxKind.StaticKeyword),
        optional: Boolean(member.questionToken),
        parameters: parameters(member),
      },
    ];
  });
}

function declarations(file) {
  const source = ts.createSourceFile(
    file,
    readFileSync(file, "utf8"),
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TS,
  );
  const path = relative(sourceRoot.pathname, file);
  return source.statements.flatMap((node) => {
    const exported = hasModifier(node, ts.SyntaxKind.ExportKeyword);
    if (ts.isFunctionDeclaration(node) && node.name) {
      return [
        {
          file: path,
          name: node.name.text,
          kind: "function",
          exported,
          async: hasModifier(node, ts.SyntaxKind.AsyncKeyword),
          parameters: parameters(node),
          members: [],
        },
      ];
    }
    if (
      ts.isClassDeclaration(node) ||
      ts.isInterfaceDeclaration(node) ||
      ts.isTypeAliasDeclaration(node) ||
      ts.isEnumDeclaration(node)
    ) {
      if (!node.name) return [];
      const kind = ts.isClassDeclaration(node)
        ? "class"
        : ts.isInterfaceDeclaration(node)
          ? "interface"
          : ts.isTypeAliasDeclaration(node)
            ? "type"
            : "enum";
      return [
        {
          file: path,
          name: node.name.text,
          kind,
          exported,
          members: membersOf(node),
        },
      ];
    }
    if (ts.isVariableStatement(node)) {
      return node.declarationList.declarations.flatMap((declaration) =>
        ts.isIdentifier(declaration.name)
          ? [
              {
                file: path,
                name: declaration.name.text,
                kind: "variable",
                exported,
                members: [],
              },
            ]
          : [],
      );
    }
    return [];
  });
}

const output = sourceFiles(sourceRoot.pathname).flatMap(declarations);
writeFileSync(process.argv[2], `${JSON.stringify(output, null, 2)}\n`);
