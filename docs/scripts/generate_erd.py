"""Render the ERD from an already migrated PostgreSQL database.

Set BURSAR_ERD_DATABASE_URL to a disposable database migrated with bursar migrate.
The database is read only; application tables and rows are never changed.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import psycopg2

OUTPUT = Path(__file__).resolve().parents[1] / "docs/concepts/database-schema.md"


def render(dsn: str) -> str:
    with (
        psycopg2.connect(dsn, options="-c default_transaction_read_only=on") as conn,
        conn.cursor() as cur,
    ):
        cur.execute("""
            SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod),
                   EXISTS (SELECT 1 FROM pg_constraint p
                           WHERE p.conrelid=c.oid AND p.contype='p' AND a.attnum=ANY(p.conkey)),
                   EXISTS (SELECT 1 FROM pg_constraint f
                           WHERE f.conrelid=c.oid AND f.contype='f' AND a.attnum=ANY(f.conkey))
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_attribute a ON a.attrelid=c.oid
            WHERE n.nspname='bursar' AND c.relkind IN ('r','p') AND NOT c.relispartition
              AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY c.relname,a.attnum
        """)
        columns = cur.fetchall()
        cur.execute("""
            SELECT child.relname, parent.relname, fk.conname,
                   ARRAY(SELECT a.attname FROM unnest(fk.conkey) WITH ORDINALITY k(attnum,ord)
                         JOIN pg_attribute a ON a.attrelid=child.oid AND a.attnum=k.attnum ORDER BY k.ord),
                   ARRAY(SELECT a.attname FROM unnest(fk.confkey) WITH ORDINALITY k(attnum,ord)
                         JOIN pg_attribute a ON a.attrelid=parent.oid AND a.attnum=k.attnum ORDER BY k.ord),
                   EXISTS (SELECT 1 FROM unnest(fk.conkey) k(attnum)
                               JOIN pg_attribute a ON a.attrelid=child.oid AND a.attnum=k.attnum
                               WHERE NOT a.attnotnull),
                   EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid=child.oid AND i.indisunique
                           AND i.indisvalid AND i.indpred IS NULL AND i.indexprs IS NULL
                           AND NOT EXISTS (
                               SELECT 1 FROM unnest(i.indkey::smallint[]) WITH ORDINALITY k(attnum, ord)
                               WHERE k.ord <= i.indnkeyatts AND NOT (k.attnum = ANY(fk.conkey))
                           ))
            FROM pg_constraint fk JOIN pg_class child ON child.oid=fk.conrelid
            JOIN pg_class parent ON parent.oid=fk.confrelid
            JOIN pg_namespace n ON n.oid=child.relnamespace
            WHERE n.nspname='bursar' AND fk.contype='f' AND fk.conparentid=0
              AND NOT child.relispartition AND NOT parent.relispartition
            ORDER BY child.relname, fk.conname
        """)
        relationships = cur.fetchall()
    lines = ["erDiagram"]
    previous = None
    for table, name, typ, pk, fk in columns:
        if table != previous:
            if previous is not None:
                lines.append("    }")
            lines.append(f"    {table} {{")
            previous = table
        # Mermaid identifiers cannot contain spaces, punctuation, or brackets.
        safe_type = re.sub(r"[^a-zA-Z0-9_]", "_", typ).strip("_") or "value"
        keys = ",".join(key for enabled, key in ((pk, "PK"), (fk, "FK")) if enabled)
        lines.append(f"        {safe_type} {name}" + (f" {keys}" if keys else ""))
    if previous is not None:
        lines.append("    }")
    for (
        child,
        parent,
        _name,
        child_cols,
        parent_cols,
        optional,
        unique,
    ) in relationships:
        parent_end = "|o" if optional else "||"
        child_end = "o|" if unique else "o{"
        label = ", ".join(child_cols) + " to " + ", ".join(parent_cols)
        lines.append(f'    {parent} {parent_end}--{child_end} {child} : "{label}"')
    return "\n".join(lines)


def main() -> None:
    dsn = os.environ.get("BURSAR_ERD_DATABASE_URL")
    if not dsn:
        sys.exit(
            "Set BURSAR_ERD_DATABASE_URL to a migrated, disposable Bursar database"
        )
    diagram = render(dsn)
    OUTPUT.write_text(
        """---
title: Database schema
sidebar_label: Database schema
description: Generated ERD for Bursar's PostgreSQL schema.
---

<!-- Generated from PostgreSQL by docs/scripts/generate_erd.py; do not edit manually. -->

# Database schema

This diagram is generated from the migrated PostgreSQL catalog. Each line is a
single foreign-key constraint; its label groups the child and parent columns in
key order. The symbols show optionality and whether the child key is unique.
Partition children are omitted because their parent defines the logical table.
Regenerate it with `BURSAR_ERD_DATABASE_URL` pointing to a disposable database
migrated with `bursar migrate`, then run `bun run generate:erd` in `docs/`.

```mermaid
"""
        + diagram
        + "\n```\n"
    )
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
