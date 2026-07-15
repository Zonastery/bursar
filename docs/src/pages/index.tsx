import type {ReactNode} from 'react';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import Link from '@docusaurus/Link';
import Heading from '@theme/Heading';
import styles from './index.module.css';

function Hero(): ReactNode {
  const {siteConfig} = useDocusaurusContext();
  return (
    <header className={styles.heroBanner}>
      <div className="container">
        <Heading as="h1" className={styles.heroTitle}>
          {siteConfig.title}
        </Heading>
        <p className={styles.heroSubtitle}>{siteConfig.tagline}</p>
        <div className={styles.buttons}>
          <Link className="button button--primary button--lg" to="/docs/intro">
            Get Started →
          </Link>
          <Link
            className="button button--secondary button--lg"
            to="https://github.com/zonastery/bursar"
            style={{marginLeft: '1rem'}}>
            GitHub
          </Link>
        </div>
      </div>
    </header>
  );
}

function Feature({title, description}: {title: string; description: string}): ReactNode {
  return (
    <div className={styles.feature}>
      <Heading as="h3">{title}</Heading>
      <p>{description}</p>
    </div>
  );
}

export default function Home(): ReactNode {
  return (
    <Layout
      title="bursar — Declarative Credit Calculation Engine"
      description="Add usage-based credits to your AI SaaS in minutes. Multi-language, database-backed pricing with a safe expression engine.">
      <Hero />
      <main className={styles.featuresSection}>
        <div className="container">
          <div className={styles.featuresGrid}>
            <Feature
              title="Multi-Language"
              description="Use the same pricing config from Python or TypeScript. Identical expression engine, identical results."
            />
            <Feature
              title="Safe Expressions"
              description="AST-based evaluator with strict allowlists. No eval(), no exec(), no arbitrary code execution."
            />
            <Feature
              title="Database-Backed"
              description="Pricing lives in bursar_config. Update live without redeploys. Dict loading for testing."
            />
            <Feature
              title="Credit Lifecycle"
              description="Reserve-then-deduct pattern with idempotency keys, reservation expiry, and min-balance enforcement."
            />
            <Feature
              title="Pluggable Stores"
              description="Supabase, PostgreSQL, or in-memory — same CreditStore interface. Bring your own backend."
            />
            <Feature
              title="Open Source"
              description="AGPL-3.0 license. Use it, fork it, contribute. <code>pip install bursar</code> or <code>npm install @zonastery/bursar</code>."
            />
          </div>
        </div>
      </main>
    </Layout>
  );
}
