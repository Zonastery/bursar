# ducto — Declarative Credit Calculation Engine

**Stack:** Pydantic (schemas), PyYAML (pricing config), Python `ast` (safe expressions). Opt. Supabase/Postgres backends.

Standalone Python library. Calculates credit costs from usage metrics (model tokens, tools, search/RAG) using YAML pricing configs. Safe expression engine — no eval/exec. Stateless, pure calculation. Used by zonastery's billing pipeline for per-request credit deduction.
