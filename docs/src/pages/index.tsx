import type { ReactNode } from "react";
import Link from "@docusaurus/Link";
import Head from "@docusaurus/Head";
import Layout from "@theme/Layout";
import CodeBlock from "@theme/CodeBlock";
import Heading from "@theme/Heading";
import styles from "./index.module.css";

const capabilities = [
  {
    number: "01",
    title: "Price every operation",
    description:
      "Define metered operations, exact-decimal rates, and plan-specific pricing in one validated configuration.",
    to: "/docs/concepts/pricing",
  },
  {
    number: "02",
    title: "Control spend before work starts",
    description:
      "Enforce allowances, quotas, entitlements, and concurrency limits in the same transaction as admission.",
    to: "/docs/guides/financial-safety",
  },
  {
    number: "03",
    title: "Maintain an auditable ledger",
    description:
      "Record grants, purchases, charges, refunds, and expiry as immutable entries with replay-safe mutations.",
    to: "/docs/concepts/data-model",
  },
  {
    number: "04",
    title: "Connect payments when needed",
    description:
      "Map provider events to subscriptions, top-ups, plan changes, and guarded auto-recharge workflows.",
    to: "/docs/guides/subscription-integration",
  },
];

const pythonExample = `from bursar import Bursar, PostgresStore

store = PostgresStore(
    database_url,
    tenant_id=tenant_id,
    provider_environment="test",
)
bursar = Bursar(credit_store=store)

charge = bursar.credits.deduct(
    account_id,
    usage_metrics,
    idempotency_key="request:0195",
)`;

const typescriptExample = `import { Bursar, PostgresStore } from "@zonastery/bursar";

const store = new PostgresStore({
  postgres: process.env.DATABASE_URL!,
  tenantId,
  providerEnvironment: "test",
});
const bursar = new Bursar({ creditStore: store });

const charge = await bursar.credits.deduct(accountId, usageMetrics, {
  idempotencyKey: "request:0195",
});`;

function CapabilityGrid(): ReactNode {
  return (
    <div className={styles.capabilityGrid}>
      {capabilities.map((capability) => (
        <Link
          className={styles.capabilityCard}
          key={capability.number}
          to={capability.to}
        >
          <span className={styles.capabilityNumber} aria-hidden="true">
            {capability.number}
          </span>
          <Heading as="h3">{capability.title}</Heading>
          <p>{capability.description}</p>
          <span className={styles.cardLink}>Read the guide</span>
        </Link>
      ))}
    </div>
  );
}

function SystemDiagram(): ReactNode {
  return (
    <div
      className={styles.systemDiagram}
      role="img"
      aria-label="Application usage, pricing configuration, and provider events flow through Bursar into a tenant-isolated PostgreSQL ledger"
    >
      <div className={styles.diagramSources}>
        <span>Application usage</span>
        <span>Pricing config</span>
        <span>Provider events</span>
      </div>
      <div className={styles.diagramConnector} aria-hidden="true">
        <span />
      </div>
      <div className={styles.diagramCore}>
        <span className={styles.diagramEyebrow}>Application boundary</span>
        <strong>Bursar</strong>
        <span>Credits · Catalog · Accounts · Billing · Commerce</span>
      </div>
      <div className={styles.diagramConnector} aria-hidden="true">
        <span />
      </div>
      <div className={styles.diagramStore}>
        <strong>PostgreSQL</strong>
        <span>Tenant isolation · Append-only ledger · Exact decimals</span>
      </div>
    </div>
  );
}

export default function Home(): ReactNode {
  return (
    <Layout
      title="Open-source AI credits and usage billing"
      description="Bursar is a PostgreSQL-native credit ledger, usage metering, and reserve-settle billing system for AI SaaS, with Python and TypeScript SDKs."
    >
      <Head>
        <title>Bursar — Open-source AI credits and usage billing</title>
      </Head>
      <main>
        <header className={styles.hero}>
          <div className={`container ${styles.heroGrid}`}>
            <div className={styles.heroCopy}>
              <p className={styles.eyebrow}>
                PostgreSQL-native Python and TypeScript SDKs
              </p>
              <Heading as="h1">
                Open-source credits and usage billing for AI SaaS
              </Heading>
              <p className={styles.lead}>
                Bursar gives AI products one exact ledger for usage pricing,
                prepaid credits, plans, allowances, and subscriptions.
              </p>
              <div className={styles.actions}>
                <Link
                  className="button button--primary button--lg"
                  to="/docs/quickstart"
                >
                  Read the quickstart
                </Link>
                <Link
                  className="button button--outline button--secondary button--lg"
                  href="https://github.com/zonastery/bursar"
                >
                  View on GitHub
                </Link>
              </div>
              <ul
                className={styles.supportList}
                aria-label="Supported platforms"
              >
                <li>Python 3.12+</li>
                <li>Node.js 22+</li>
                <li>PostgreSQL 16+</li>
                <li>AGPL-3.0</li>
              </ul>
            </div>
            <div className={styles.heroCode}>
              <div className={styles.codeHeader}>
                <span>One workflow, two SDKs</span>
                <span className={styles.status}>Current stable</span>
              </div>
              <CodeBlock language="python" title="Python">
                {pythonExample}
              </CodeBlock>
            </div>
          </div>
        </header>

        <section
          className={styles.section}
          aria-labelledby="capabilities-heading"
        >
          <div className="container">
            <div className={styles.sectionHeading}>
              <p className={styles.eyebrow}>Complete billing lifecycle</p>
              <Heading id="capabilities-heading" as="h2">
                Keep pricing, access, and money state consistent
              </Heading>
              <p>
                Each capability shares the same versioned configuration, tenant
                boundary, and transactional ledger.
              </p>
            </div>
            <CapabilityGrid />
          </div>
        </section>

        <section
          className={`${styles.section} ${styles.systemSection}`}
          aria-labelledby="system-heading"
        >
          <div className={`container ${styles.systemGrid}`}>
            <div className={styles.sectionHeading}>
              <p className={styles.eyebrow}>One system of record</p>
              <Heading id="system-heading" as="h2">
                Put the financial boundary behind one facade
              </Heading>
              <p>
                Bursar prices usage, checks policy, and commits the result in
                one transaction. Payment integrations remain optional.
              </p>
              <Link
                className={styles.textLink}
                to="/docs/concepts/architecture"
              >
                Explore the architecture
              </Link>
            </div>
            <SystemDiagram />
          </div>
        </section>

        <section className={styles.section} aria-labelledby="sdk-heading">
          <div className="container">
            <div className={styles.sectionHeading}>
              <p className={styles.eyebrow}>Behavioral parity</p>
              <Heading id="sdk-heading" as="h2">
                Use the same model from Python and TypeScript
              </Heading>
              <p>
                Both SDKs share the configuration schema, expression fixtures,
                PostgreSQL migrations, rounding rules, and error contract.
              </p>
            </div>
            <div className={styles.sdkGrid}>
              <div className={styles.sdkPanel}>
                <div className={styles.sdkPanelHeader}>
                  <strong>Python</strong>
                  <code>python -m pip install "bursar[postgres]"</code>
                </div>
                <CodeBlock language="python">{pythonExample}</CodeBlock>
                <Link to="/docs/python-api">Browse the Python API</Link>
              </div>
              <div className={styles.sdkPanel}>
                <div className={styles.sdkPanelHeader}>
                  <strong>TypeScript</strong>
                  <code>npm install @zonastery/bursar pg</code>
                </div>
                <CodeBlock language="typescript">{typescriptExample}</CodeBlock>
                <Link to="/docs/javascript-api">Browse the TypeScript API</Link>
              </div>
            </div>
          </div>
        </section>

        <section className={styles.cta} aria-labelledby="cta-heading">
          <div className={`container ${styles.ctaInner}`}>
            <div>
              <p className={styles.eyebrow}>Start with an isolated ledger</p>
              <Heading id="cta-heading" as="h2">
                Build the first metered charge
              </Heading>
              <p>
                Install the schema, publish a config, create an account, and
                inspect the resulting ledger entry.
              </p>
            </div>
            <div className={styles.actions}>
              <Link
                className="button button--primary button--lg"
                to="/docs/quickstart"
              >
                Open the quickstart
              </Link>
              <Link
                className="button button--outline button--secondary button--lg"
                to="/docs/tutorials"
              >
                Browse tutorials
              </Link>
            </div>
          </div>
        </section>
      </main>
    </Layout>
  );
}
