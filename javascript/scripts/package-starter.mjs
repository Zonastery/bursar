import { copyFileSync } from "node:fs";

copyFileSync(
  new URL("../../python/src/bursar/starter.yaml", import.meta.url),
  new URL("../dist/starter.yaml", import.meta.url),
);
