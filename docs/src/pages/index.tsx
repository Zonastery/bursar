import type {ReactNode} from 'react';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import Link from '@docusaurus/Link';
import Heading from '@theme/Heading';

function Hero(): ReactNode {
  const {siteConfig} = useDocusaurusContext();
  return (
    <header className="hero hero--primary">
      <div className="container">
        <Heading as="h1" className="hero__title">
          {siteConfig.title}
        </Heading>
        <p className="hero__subtitle">{siteConfig.tagline}</p>
        <div className="margin-top--lg">
          <Link
            className="button button--secondary button--lg margin-right--md"
            to="/docs/intro">
            Get Started
          </Link>
          <Link
            className="button button--secondary button--lg"
            to="https://github.com/zonastery/bursar">
            GitHub
          </Link>
        </div>
      </div>
    </header>
  );
}

function Feature({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}): ReactNode {
  return (
    <div className="col col--4 margin-bottom--lg">
      <div className="card">
        <div className="card__header">
          <Heading as="h3">{title}</Heading>
        </div>
        <div className="card__body">{children}</div>
      </div>
    </div>
  );
}

export default function Home(): ReactNode {
  return (
    <Layout
      title="bursar — Declarative Credit Calculation Engine"
      description="Add usage-based credits to your AI SaaS in minutes. Multi-language, database-backed pricing with a safe expression engine.">
      <Hero />
      <main>
        <section className="margin-vert--xl">
          <div className="container">
            <div className="row">
              <Feature title="Multi-Language">
                Use the same pricing config from Python or TypeScript. Identical
                expression engine, identical results.
              </Feature>
              <Feature title="Safe Expressions">
                AST-based evaluator with strict allowlists. No eval(), no
                exec(), no arbitrary code execution.
              </Feature>
              <Feature title="Database-Backed">
                Pricing lives in <code>bursar_config</code>. Update live
                without redeploys. Dict loading for testing.
              </Feature>
              <Feature title="Credit Lifecycle">
                Reserve-then-deduct pattern with idempotency keys, reservation
                expiry, and min-balance enforcement.
              </Feature>
              <Feature title="PostgreSQL-First">
                One canonical schema, applied by <code>bursar migrate</code>. The
                <code>CreditStore</code> abstraction keeps room for custom
                stores.
              </Feature>
              <Feature title="Open Source">
                AGPL-3.0 license. Use it, fork it, contribute.{' '}
                <code>pip install bursar</code> or{' '}
                <code>npm install @zonastery/bursar</code>.
              </Feature>
            </div>
          </div>
        </section>
      </main>
    </Layout>
  );
}
