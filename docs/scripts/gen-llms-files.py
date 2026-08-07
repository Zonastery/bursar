#!/usr/bin/env python3
"""Generate static/llms.txt and static/llms-full.txt for the bursar docs site.

The files follow the llms.txt spec (https://llmstxt.org): ``llms.txt`` is a
curated markdown index of the most useful pages with absolute URLs, and
``llms-full.txt`` concatenates the full text of every page linked from the
main (non-Optional) sections.

Run on every build via the ``prebuild``/``prestart`` npm scripts. The outputs
land in ``static/`` so Docusaurus serves them at the site root
(``/bursar/llms.txt``, ``/bursar/llms-full.txt``).
"""
import re
import sys
from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parents[1]
PAGES_DIR = DOCS_DIR / "docs"
STATIC_DIR = DOCS_DIR / "static"

SITE_URL = "https://zonastery.github.io/bursar"

# Section title -> list of (slug_or_url, fallback_description, display_name,
# index_description_override). A slug maps to a page under docs/docs/;
# anything else is treated as an external URL (Optional section only) and is
# not inlined in llms-full.txt. display_name is optional; when omitted it is
# derived from the slug. index_description_override, when given, replaces the
# page's frontmatter description in llms.txt (frontmatter descriptions are
# written for SEO and can exceed the ~120-char llms.txt guidance).
MANIFEST: list[tuple[str, list[tuple[str, str | None, str | None, str | None]]]] = [
    (
        "Getting Started",
        [
            ("intro", None, "Introduction", "Reusable credit ledger and billing engine for AI SaaS — one config, two SDKs, exact money on one PostgreSQL schema."),
            ("quickstart", None, "Quickstart", "Ten minutes to a working credit ledger — install the schema, publish a config, grant credits, and charge for usage."),
            ("agent-skills", None, "Agent Skills", "Install the bursar skill for coding agents in one command — metering, credits, plans, quotas, and billing done right."),
        ],
    ),
    (
        "Core Concepts",
        [
            ("concepts/data-model", None, "Data model", "Accounts, the append-only ledger, lots, leases, and allowances — mapped to tables on one PostgreSQL schema."),
            ("concepts/configuration", None, "Configuration", "The strict, versioned BursarConfig document — operations, rate cards, credits, plans, and validation."),
            ("concepts/pricing", None, "Pricing", None),
            ("concepts/expressions", None, "Expressions", "The safe expression language for pricing formulas — allowlisted functions and operators, shared by both SDKs."),
            ("concepts/plans", None, "Plans", "What a paid tier gets — allowed operations, rate cards, free credit allowances, features, and quotas."),
            ("concepts/billing", None, "Billing", "How billing connects the credit ledger to a payment provider — offers, events, and auto-recharge guardrails."),
            ("concepts/architecture", None, "Architecture", "The Bursar facade and its capabilities — credits, catalog, accounts, billing, and commerce over the Postgres schema."),
        ],
    ),
    (
        "Guides",
        [
            ("guides/credit-lifecycle", None, "Credit lifecycle", "Follow one user's credits from signup to ledger — grants, purchases, charges, refunds, expiry, and revocation."),
            ("guides/financial-safety", None, "Financial safety"),
            ("guides/multitenancy", None, "Multitenancy"),
            ("guides/subscription-integration", None, "Subscription integration"),
            ("guides/storage-backends", None, "Storage backends", "PostgreSQL is the production credit store; ClickHouse and S3 adapters ingest high-cardinality data through the outbox."),
        ],
    ),
    (
        "CLI",
        [
            ("cli", None, "CLI"),
        ],
    ),
    (
        "Python API",
        [
            ("python-api/index", None, "Overview"),
            ("python-api/pricing-engine", None, "PricingEngine", "The bursar PricingEngine — database-free pricing from a canonical config, with UsageMetrics and CostBreakdown results."),
            ("python-api/credit-manager", None, "Credit manager", "The bursar credits service — balances, metered charging, leases, plans, ledger history, analytics, and teams."),
            ("python-api/stores", None, "Stores"),
        ],
    ),
    (
        "JavaScript API",
        [
            ("javascript-api/index", None, "Overview"),
            ("javascript-api/pricing-engine", None, "PricingEngine", "The bursar PricingEngine — database-free pricing from a canonical config, with UsageMetrics and CostBreakdown results."),
            ("javascript-api/credit-manager", None, "Credit manager", "The async bursar credits service — balances, metered charging, leases, plans, ledger history, analytics, teams."),
            ("javascript-api/stores", None, "Stores"),
        ],
    ),
    (
        "Examples",
        [
            ("notebooks/why_bursar_and_setup", "End-to-end setup: install, schema migration, tenant creation, and the first credit grant.", "Why bursar and setup"),
            ("notebooks/first_pricing_config", "Write, validate, and publish the canonical pricing config.", "First pricing config"),
            ("notebooks/pricing_engine", "Price UsageMetrics without a database using the PricingEngine.", "Pricing engine"),
            ("notebooks/expression_language", "The safe expression language for rate cards and price rules.", "Expression language"),
            ("notebooks/credit_lifecycle", "Grants, charges, refunds, expiry, and revocation as ledger entries.", "Credit lifecycle"),
            ("notebooks/plans_and_allowances", "Free plans, monthly allowances, and admission policies.", "Plans and allowances"),
            ("notebooks/quotas_and_spend_caps", "Quotas and hard spend caps enforced atomically.", "Quotas and spend caps"),
            ("notebooks/credit_tiers_and_expiry", "Tiered credit buckets and expiry windows.", "Credit tiers and expiry"),
            ("notebooks/leases_and_financial_safety", "Atomic reserve/settle leases for long-running jobs.", "Leases and financial safety"),
            ("notebooks/teams", "Multi-user accounts, member spend caps, and team charging.", "Teams"),
            ("notebooks/analytics", "Ledger analytics, spend by user and model, and aggregation queries.", "Analytics"),
            ("notebooks/events", "Credit lifecycle events and event delivery.", "Events"),
            ("notebooks/subscriptions_and_auto_recharge", "Subscriptions and auto-recharge guardrails.", "Subscriptions and auto-recharge"),
            ("notebooks/cli_and_deployment", "The bursar CLI and deploying configs in production.", "CLI and deployment"),
            ("notebooks/custom_stores", "Roll your own CreditStore backend.", "Custom stores"),
            ("notebooks/pricing_config_schema", "The full pricing config schema, end to end.", "Pricing config schema"),
        ],
    ),
    (
        "Optional",
        [
            ("python-api/reference", "By-module symbol reference, auto-generated from the Python SDK.", "Python API reference (generated)"),
            ("javascript-api/reference", "By-symbol reference, auto-generated from the TypeScript SDK.", "JavaScript API reference (generated)"),
            ("concepts/database-schema", "Generated ERD diagram of the PostgreSQL schema.", "Database schema (ERD)"),
            ("https://github.com/zonastery/bursar", "Source code, issues, and discussions.", "GitHub"),
            ("https://pypi.org/project/bursar/", "Python package on PyPI.", "PyPI"),
            ("https://www.npmjs.com/package/@zonastery/bursar", "TypeScript package on npm.", "npm"),
        ],
    ),
]

SUMMARY = (
    "Declarative credit calculation and billing engine for AI SaaS — metering, "
    "prepaid credits, plans and allowances, and subscriptions over one "
    "PostgreSQL schema, from identical Python and JavaScript SDKs."
)


def page_file(slug: str) -> Path | None:
    for suffix in (".mdx", ".md"):
        candidate = PAGES_DIR / f"{slug}{suffix}"
        if candidate.exists():
            return candidate
    # Generated notebook pages carry a numeric prefix (00_why_bursar_and_setup.mdx)
    # while their slugs strip it (notebooks/why_bursar_and_setup).
    parent, _, name = slug.rpartition("/")
    matches = sorted((PAGES_DIR / parent).glob(f"*_{name}.mdx")) if parent else []
    return matches[0] if matches else None


def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not match:
        return {}
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


def clean_mdx(text: str) -> str:
    """Convert Docusaurus MDX source into plain readable markdown."""
    # Front matter
    text = re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.S)
    # HTML comments
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    # import statements
    text = re.sub(r"^import .*?;\s*\n?", "", text, flags=re.M)
    # Code fences with titles: ```bash title="Terminal" -> ```bash
    text = re.sub(r"^(```\w*)\s+title=\"[^\"]*\"", r"\1", text, flags=re.M)
    # JSX images: useBaseUrl() and className are meaningless outside Docusaurus.
    # The src is not resolvable in plain markdown, so fall back to the alt text.
    text = re.sub(
        r"<img\b[^>]*?>",
        lambda m: f"*{a.group(1)}*\n" if (a := re.search(r'alt="([^"]*)"', m.group(0))) else "",
        text,
        flags=re.S,
    )
    # Tabs components: drop the wrapper, keep the inner content. Component tags
    # may be indented in the source; consume the indentation so following lines
    # (e.g. admonitions) stay at column 0.
    text = re.sub(r"^[ \t]*<Tabs[^>]*>[ \t]*\n?", "", text, flags=re.M)
    text = re.sub(r"^[ \t]*</Tabs>[ \t]*\n?", "", text, flags=re.M)
    # TabItem: keep content, label it so the language split stays readable
    text = re.sub(
        r"^[ \t]*<TabItem[^>]*label=\"([^\"]+)\"[^>]*>[ \t]*\n?",
        r"**\1:**\n",
        text,
        flags=re.M,
    )
    text = re.sub(r"^[ \t]*</TabItem>[ \t]*\n?", "", text, flags=re.M)
    # Docusaurus admonitions (:::note / :::info Title ... :::) -> bold labels
    text = re.sub(
        r"^[ \t]*:::(note|tip|info|warning|caution|danger)(.*)$",
        lambda m: f"**{m.group(1).title()}:**{m.group(2).rstrip()}\n",
        text,
        flags=re.M,
    )
    text = re.sub(r"^[ \t]*:::+[ \t]*$", "", text, flags=re.M)
    # Collapse runs of blank lines left by removed markup
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def entry_url(entry: str) -> str:
    if entry.startswith("http"):
        return entry
    return f"{SITE_URL}/docs/{entry}"


def entry_name(entry: str, name: str | None) -> str:
    if name:
        return name
    if entry.startswith("http"):
        name = entry.rstrip("/").rsplit("/", 1)[-1]
        return name or entry
    return entry.rsplit("/", 1)[-1]


def describe(entry: str, fallback: str | None, override: str | None) -> str:
    """Page description for llms.txt. Precedence: manifest override, frontmatter
    description, manifest fallback, page title."""
    if override:
        return override
    if entry.startswith("http"):
        return fallback or entry
    path = page_file(entry)
    if path is None:
        # Slugs without a physical page (generated-index landing pages) fall
        # back to their manifest description.
        if fallback:
            return fallback
        raise SystemExit(f"gen-llms-files: missing page for slug {entry!r}")
    fields = frontmatter(path)
    if fields.get("description"):
        return fields["description"]
    if fallback:
        return fallback
    if fields.get("title"):
        return fields["title"]
    raise SystemExit(f"gen-llms-files: no description available for {entry!r}")


def _pad(entry: tuple[str, ...]) -> tuple[str, str | None, str | None, str | None]:
    """Pad a manifest entry to the 4-tuple (slug, fallback, name, override)."""
    return (entry + (None,) * (4 - len(entry)))[:4]  # type: ignore[return-value]


def render_index() -> str:
    lines = [
        "# bursar",
        "",
        f"> {SUMMARY}",
        "",
        "Bursar gives AI applications one place to meter usage, sell prepaid "
        "credits, run plans and allowances, and orchestrate subscriptions. A "
        "single strict versioned config drives pricing operations, rate cards, "
        "credit buckets, entitlements, plans, quotas, and auto-recharge "
        "guardrails; all money is exact decimal on an append-only PostgreSQL "
        "ledger, served by identical Python and JavaScript SDKs. Start with "
        "the Quickstart, then read the data model, pricing, plans, and "
        "configuration concepts.",
        "",
    ]
    for section, entries in MANIFEST:
        lines.append(f"## {section}")
        lines.append("")
        for entry, fallback, name, override in map(_pad, entries):
            lines.append(f"- [{entry_name(entry, name)}]({entry_url(entry)}): {describe(entry, fallback, override)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_full() -> tuple[str, int]:
    """Concatenated full text of every page linked from the main sections.
    Returns the rendered text and the number of pages inlined."""
    header = (
        "# bursar — full documentation\n"
        f"\n> {SUMMARY}\n"
        "\nThis file concatenates the full text of every page linked from the "
        "main sections of llms.txt, in reading order, so LLMs can ingest the "
        "whole documentation corpus in one pass. For the curated link index, "
        "see llms.txt; for auto-generated per-symbol API reference, see the "
        "Optional section there."
    )
    pages: list[str] = []
    count = 0
    for section, entries in MANIFEST:
        if section == "Optional":
            continue
        for entry, _fallback, _name, _override in map(_pad, entries):
            path = page_file(entry)
            if path is None:
                raise SystemExit(f"gen-llms-files: missing page for slug {entry!r}")
            pages.append(
                f"> Source: {entry_url(entry)}\n\n"
                + clean_mdx(path.read_text(encoding="utf-8"))
            )
            count += 1
    return header + "\n\n---\n\n" + "\n---\n\n".join(pages).rstrip() + "\n", count


def validate_index(text: str) -> list[str]:
    errors: list[str] = []

    def fail(msg: str) -> None:
        errors.append(msg)

    lines = text.splitlines()
    first_non_blank = next((ln for ln in lines if ln.strip()), "")
    if not first_non_blank.startswith("# "):
        fail("llms.txt must start with exactly one H1 heading")
    h1_count = sum(1 for ln in lines if re.match(r"^# ", ln))
    if h1_count != 1:
        fail(f"llms.txt must contain exactly one H1, found {h1_count}")

    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    idx += 1  # past the H1
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines) or not lines[idx].startswith("> "):
        fail("llms.txt must have a blockquote summary immediately after the H1")

    section_open = False
    for line in lines:
        if re.match(r"^## ", line):
            section_open = True
            continue
        if not line.strip():
            continue
        if section_open and re.match(r"^- \[", line):
            m = re.match(r"^- \[[^\]]+\]\((https?://[^)]+)\):?\s?.*$", line)
            if not m:
                fail(f"malformed link line (must be absolute URL): {line}")
        elif section_open and not line.startswith("> "):
            fail(f"content inside section without heading/list context: {line!r}")
    return errors


def main() -> int:
    index = render_index()
    full, count = render_full()

    errors = validate_index(index)
    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1

    # Every page linked from the main sections must be inlined in llms-full.txt.
    for section, entries in MANIFEST:
        if section == "Optional":
            continue
        for entry, _fallback, _name, _override in map(_pad, entries):
            source_line = f"> Source: {entry_url(entry)}"
            if source_line not in full:
                print(f"ERROR: {source_line} missing from llms-full.txt", file=sys.stderr)
                return 1

    # Non-fatal best-practice check: llms.txt descriptions should be brief.
    for section, entries in MANIFEST:
        for entry, fallback, name, override in map(_pad, entries):
            desc = describe(entry, fallback, override)
            if len(desc) > 120:
                print(f"WARNING: description for {name or entry} is {len(desc)} chars "
                      "(>120, llmstxt.org asks for brief descriptions)", file=sys.stderr)

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    (STATIC_DIR / "llms.txt").write_text(index, encoding="utf-8")
    (STATIC_DIR / "llms-full.txt").write_text(full, encoding="utf-8")

    print(f"Wrote static/llms.txt ({len(index)} bytes, {index.count(chr(10) + '- [')} links)")
    print(f"Wrote static/llms-full.txt ({len(full)} bytes, {count} pages inlined)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
