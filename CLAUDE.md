# ducto -- Declarative Credit Calculation Engine

**Stack:** Pydantic (schemas), Python `ast` (safe expressions). Opt. Supabase/Postgres backends.

Standalone Python library. Calculates credit costs from usage metrics (model tokens, tools, search/RAG) using DB-backed pricing expressions from `credit_pricing_config` table (JSONB). `from_dict()` available for quick-start/testing. Safe expression engine -- no eval/exec. Stateless, pure calculation.
