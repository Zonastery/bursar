export interface NumNode {
  type: "number";
  /** Exact literal text, parsed directly by Decimal without a binary-float round trip. */
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
  NumNode | IdentNode | BinOpNode | UnaryNode | CallNode | TernaryNode | CompareNode | BoolOpNode;

export function children(node: Node): Node[] {
  switch (node.type) {
    case "binary":
    case "comparison":
    case "boolean":
      return [node.left, node.right];
    case "unary":
      return [node.operand];
    case "call":
      return node.args;
    case "ternary":
      return [node.cond, node.then, node.else];
    case "number":
    case "identifier":
      return [];
  }
}
