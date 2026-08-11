import { themes as prismThemes } from "prism-react-renderer";
import type { Config } from "@docusaurus/types";
import type * as Preset from "@docusaurus/preset-classic";

const repositoryUrl = "https://github.com/Zonastery/bursar";
const siteUrl = "https://zonastery.github.io/bursar/";
const siteTitle = "Bursar AI Credits";
const corpusTitle = "Bursar — Open-source AI credits and usage billing";
const siteDescription =
  "PostgreSQL-native usage metering, prepaid credits, plans, and reserve-settle billing for AI SaaS, available as Python, TypeScript, and Go SDKs.";

const structuredData = {
  "@context": "https://schema.org",
  "@graph": [
    {
      "@type": "WebSite",
      "@id": `${siteUrl}#website`,
      name: "Bursar",
      alternateName: "Bursar AI credits and usage billing",
      url: siteUrl,
      description: siteDescription,
      inLanguage: "en",
    },
    {
      "@type": "SoftwareSourceCode",
      "@id": `${repositoryUrl}#software`,
      name: "Bursar",
      alternateName: "Bursar AI Credits SDK",
      description: siteDescription,
      url: siteUrl,
      codeRepository: repositoryUrl,
      license: `${repositoryUrl}/blob/main/LICENSE`,
      programmingLanguage: ["Python", "TypeScript", "Go", "SQL"],
      runtimePlatform: [
        "Python 3.12–3.13",
        "Node.js 22+",
        "Go 1.25+",
        "PostgreSQL 16+",
      ],
      isAccessibleForFree: true,
      sameAs: [
        "https://pypi.org/project/bursar/",
        "https://www.npmjs.com/package/@zonastery/bursar",
        "https://pkg.go.dev/github.com/Zonastery/bursar/golang/v2",
      ],
    },
  ],
};

const config: Config = {
  title: siteTitle,
  tagline: "PostgreSQL-native credits, usage metering, and billing for AI SaaS",
  favicon: "img/logo.png",

  url: "https://zonastery.github.io",
  baseUrl: "/bursar/",
  trailingSlash: false,

  organizationName: "zonastery",
  projectName: "bursar",

  future: {
    // Adopt the Docusaurus 4 compatibility flags early while retaining the
    // stable bundler. @docusaurus/faster remains experimental in 3.10.
    v4: true,
    faster: false,
  },

  onBrokenLinks: "throw",
  onBrokenAnchors: "throw",
  onDuplicateRoutes: "throw",
  markdown: {
    format: "detect",
    mermaid: true,
    hooks: {
      onBrokenMarkdownLinks: "throw",
      onBrokenMarkdownImages: "throw",
    },
  },

  i18n: {
    defaultLocale: "en",
    locales: ["en"],
  },

  plugins: [
    [
      "docusaurus-plugin-typedoc",
      {
        entryPoints: [
          "../javascript/src/index.ts",
          "../javascript/src/node.ts",
          "../javascript/src/telemetry/opentelemetry.ts",
        ],
        tsconfig: "../javascript/tsconfig.json",
        out: "docs/javascript-api/reference",
        sidebar: { autoConfiguration: false },
      },
    ],
    [
      "docusaurus-plugin-llms",
      {
        title: corpusTitle,
        description: siteDescription,
        version: "2.x",
        docsDir: "docs",
        generateLLMsTxt: true,
        generateLLMsFullTxt: true,
        generateMarkdownFiles: true,
        excludeImports: true,
        removeDuplicateHeadings: true,
        ignoreFiles: ["python-api/reference/**", "javascript-api/reference/**"],
        includeOrder: [
          "intro.mdx",
          "quickstart.mdx",
          "notebooks/foundations/**/*.mdx",
          "notebooks/credits-and-controls/**/*.mdx",
          "notebooks/billing-and-operations/**/*.mdx",
          "guides/index.mdx",
          "guides/multitenancy.mdx",
          "guides/storage-backends.mdx",
          "guides/ai-saas-credits.mdx",
          "guides/financial-safety.mdx",
          "guides/credit-lifecycle.mdx",
          "guides/subscription-integration.mdx",
          "guides/opentelemetry.mdx",
          "guides/google-adk.mdx",
          "agent-skills.mdx",
          "concepts/index.mdx",
          "concepts/architecture.mdx",
          "concepts/data-model.mdx",
          "concepts/pricing.mdx",
          "concepts/plans.mdx",
          "concepts/billing.mdx",
          "concepts/configuration.mdx",
          "concepts/expressions.mdx",
          "concepts/database-schema.md",
          "cli.mdx",
          "python-api/**/*.mdx",
          "javascript-api/**/*.mdx",
          "go-api/**/*.mdx",
        ],
        includeUnmatchedLast: false,
        rootContent:
          "Start with the quickstart. Use tutorials to learn the system, how-to guides to complete production tasks, concepts to understand design decisions, and reference pages for exact API details.",
      },
    ],
    [
      "@easyops-cn/docusaurus-search-local",
      {
        indexDocs: true,
        indexBlog: false,
        indexPages: true,
        hashed: true,
        language: ["en"],
      },
    ],
  ],

  headTags: [
    {
      tagName: "meta",
      attributes: {
        name: "google-site-verification",
        content: "IWTuIskdYhoIL0MCB517DjpPh6I4uaZrRB3WQaCpCaU",
      },
    },
    {
      tagName: "script",
      attributes: {
        type: "application/ld+json",
      },
      innerHTML: JSON.stringify(structuredData),
    },
  ],

  themes: ["@docusaurus/theme-mermaid"],

  presets: [
    [
      "classic",
      {
        docs: {
          sidebarPath: "./sidebars.ts",
          lastVersion: "current",
          versions: {
            current: {
              label: "2.x",
              badge: true,
              banner: "none",
            },
          },
          editUrl: ({ versionDocsDirPath, docPath }) =>
            `${repositoryUrl}/edit/main/docs/${versionDocsDirPath}/${docPath}`,
        },
        blog: false,
        sitemap: {
          changefreq: "weekly",
          priority: 0.5,
          ignorePatterns: ["/docs/tags/**"],
        },
        theme: {
          customCss: "./src/css/custom.css",
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    image: "img/social-card.png",
    metadata: [
      { name: "description", content: siteDescription },
      {
        name: "keywords",
        content:
          "AI SaaS billing, AI credits, usage-based billing, usage metering, prepaid credits, credit ledger, LLM billing, PostgreSQL, Python SDK, TypeScript SDK, Go SDK",
      },
      { name: "twitter:card", content: "summary_large_image" },
      { property: "og:type", content: "website" },
      { property: "og:site_name", content: "Bursar" },
      { name: "theme-color", content: "#102a43" },
    ],
    mermaid: {
      theme: { light: "neutral", dark: "dark" },
      options: {
        maxTextSize: 100000,
      },
    },
    docs: {
      sidebar: {
        hideable: true,
        autoCollapseCategories: true,
      },
    },
    tableOfContents: {
      minHeadingLevel: 2,
      maxHeadingLevel: 3,
    },
    colorMode: {
      defaultMode: "light",
      respectPrefersColorScheme: true,
    },
    navbar: {
      title: "Bursar",
      logo: {
        alt: "Bursar",
        src: "img/logo.png",
        width: 32,
        height: 32,
      },
      hideOnScroll: true,
      items: [
        {
          type: "docSidebar",
          sidebarId: "docs",
          position: "left",
          label: "Documentation",
        },
        {
          to: "/docs/notebooks/why_bursar_and_setup",
          position: "left",
          label: "Tutorials",
        },
        {
          label: "API reference",
          position: "left",
          items: [
            { label: "Python", to: "/docs/python-api" },
            { label: "TypeScript", to: "/docs/javascript-api" },
            { label: "Go", to: "/docs/go-api" },
            { label: "Command-line interface", to: "/docs/cli" },
            { label: "Database schema", to: "/docs/concepts/database-schema" },
          ],
        },
        {
          href: `${repositoryUrl}/releases`,
          position: "right",
          label: "2.x",
          "aria-label": "Bursar 2.x releases",
        },
        {
          href: repositoryUrl,
          position: "right",
          className: "header-github-link",
          "aria-label": "Bursar on GitHub",
        },
      ],
    },
    footer: {
      style: "dark",
      links: [
        {
          title: "Documentation",
          items: [
            { label: "Quickstart", to: "/docs/quickstart" },
            { label: "Tutorials", to: "/docs/notebooks/why_bursar_and_setup" },
            { label: "How-to guides", to: "/docs/guides" },
            { label: "Concepts", to: "/docs/concepts" },
          ],
        },
        {
          title: "Reference",
          items: [
            { label: "Python API", to: "/docs/python-api" },
            { label: "TypeScript API", to: "/docs/javascript-api" },
            { label: "Go API", to: "/docs/go-api" },
            { label: "CLI", to: "/docs/cli" },
            { label: "Database schema", to: "/docs/concepts/database-schema" },
          ],
        },
        {
          title: "Project",
          items: [
            { label: "GitHub", href: repositoryUrl },
            {
              label: "Contributing",
              href: `${repositoryUrl}/blob/main/CONTRIBUTING.md`,
            },
            {
              label: "Code of conduct",
              href: `${repositoryUrl}/blob/main/CODE_OF_CONDUCT.md`,
            },
            { label: "Security", href: `${repositoryUrl}/security/policy` },
            { label: "Releases", href: `${repositoryUrl}/releases` },
          ],
        },
        {
          title: "Packages",
          items: [
            { label: "PyPI", href: "https://pypi.org/project/bursar/" },
            {
              label: "npm",
              href: "https://www.npmjs.com/package/@zonastery/bursar",
            },
            {
              label: "Go",
              href: "https://pkg.go.dev/github.com/Zonastery/bursar/golang/v2",
            },
            { label: "Agent skill", to: "/docs/agent-skills" },
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} Zonastery contributors. Bursar is licensed under AGPL-3.0.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ["go", "python", "bash", "json", "yaml", "sql"],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
