# Bursar config revamp

Design and implementation docs for restructuring the bursar pricing config and
propagating the changes end-to-end through both SDKs, SQL, and public API.

The module is **unreleased** — we keep **`version: 1`** as the only valid schema
version integer and redesign the entire config shape and codebase in place. There
is no backward-compatibility layer and no `version: 2` bump.

| Document | Purpose |
|----------|---------|
| [schema-revamp.md](schema-revamp.md) | Target schema, section reference, rename map, validation rules |
| [implementation-plan.md](implementation-plan.md) | Phased file-level build plan (Python, JS, SQL, tests, docs) |
