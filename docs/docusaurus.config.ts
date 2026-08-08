import { themes as prismThemes } from "prism-react-renderer";
import type { Config } from "@docusaurus/types";
import type * as Preset from "@docusaurus/preset-classic";

const repositoryUrl = "https://github.com/zonastery/bursar";
const siteDescription =
  "Usage metering, prepaid credits, plans, and billing for AI products, backed by an exact PostgreSQL ledger.";

const config: Config = {
  title: "Bursar",
  tagline: "Usage metering and billing infrastructure for AI products",
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
        entryPoints: ["../javascript/src/index.ts"],
        tsconfig: "../javascript/tsconfig.json",
        out: "docs/javascript-api/reference",
        sidebar: { autoConfiguration: false },
      },
    ],
    [
      "docusaurus-plugin-llms",
      {
        title: "Bursar documentation",
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
          "guides/financial-safety.mdx",
          "guides/credit-lifecycle.mdx",
          "guides/subscription-integration.mdx",
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
      { name: "twitter:card", content: "summary_large_image" },
      { property: "og:type", content: "website" },
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
            { label: "Agent skill", to: "/docs/agent-skills" },
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} Zonastery contributors. Bursar is licensed under AGPL-3.0.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ["python", "bash", "json", "yaml", "sql"],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
