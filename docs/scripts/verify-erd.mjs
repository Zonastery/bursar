import { readFileSync } from "node:fs";
import mermaid from "mermaid";

const doc = readFileSync(
  new URL("../docs/concepts/database-schema.md", import.meta.url),
  "utf8",
);
const match = doc.match(/```mermaid\n([\s\S]*?)\n```/);
if (!match) throw new Error("Database schema Mermaid diagram is missing");
await mermaid.parse(match[1]);
