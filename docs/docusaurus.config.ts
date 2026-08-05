import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

const config: Config = {
  title: 'bursar',
  tagline: 'Declarative Credit Calculation Engine for AI SaaS',
  favicon: 'img/favicon.ico',

  url: 'https://zonastery.github.io',
  baseUrl: '/bursar/',

  organizationName: 'zonastery',
  projectName: 'bursar',

  onBrokenLinks: 'throw',
  markdown: {
    format: 'detect',
    mermaid: true,
    hooks: {
      onBrokenMarkdownLinks: 'warn',
    },
  },

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  plugins: [
    [
      'docusaurus-plugin-typedoc',
      {
        entryPoints: ['../javascript/src/index.ts'],
        tsconfig: '../javascript/tsconfig.json',
        out: 'docs/javascript-api/reference',
        sidebar: {autoConfiguration: false},
      },
    ],
    [
      '@easyops-cn/docusaurus-search-local',
      {
        indexDocs: true,
        indexBlog: false,
        indexPages: false,
        hashed: true,
        language: ['en'],
      },
    ],
  ],

  themes: ['@docusaurus/theme-mermaid'],

  presets: [
    [
      'classic',
      {
        docs: {
          sidebarPath: './sidebars.ts',
          showLastUpdateTime: true,
          editUrl:
            'https://github.com/zonastery/bursar/tree/main/docs/',
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    image: 'img/social-card.png',
    metadata: [{name: 'twitter:card', content: 'summary_large_image'}],
    mermaid: {
      theme: {light: 'neutral', dark: 'dark'},
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
    colorMode: {
      respectPrefersColorScheme: true,
    },
    navbar: {
      title: 'bursar',
      logo: {
        alt: 'Bursar logo',
        src: 'img/logo.png',
        width: 40,
        height: 40,
      },
      hideOnScroll: true,
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'docs',
          position: 'left',
          label: 'Docs',
        },
        {
          href: 'https://github.com/zonastery/bursar',
          position: 'right',
          className: 'header-github-link',
          'aria-label': 'GitHub repository',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Docs',
          items: [
            {
              label: 'Getting Started',
              to: '/docs/quickstart',
            },
            {
              label: 'Core concepts',
              to: '/docs/concepts/data-model',
            },
            {
              label: 'Guides',
              to: '/docs/guides/credit-lifecycle',
            },
            {
              label: 'Python API',
              to: '/docs/python-api',
            },
            {
              label: 'JavaScript API',
              to: '/docs/javascript-api',
            },
          ],
        },
        {
          title: 'Community',
          items: [
            {
              label: 'GitHub Issues',
              href: 'https://github.com/zonastery/bursar/issues',
            },
            {
              label: 'GitHub Discussions',
              href: 'https://github.com/zonastery/bursar/discussions',
            },
          ],
        },
        {
          title: 'More',
          items: [
            {
              label: 'GitHub',
              href: 'https://github.com/zonastery/bursar',
            },
            {
              label: 'PyPI',
              href: 'https://pypi.org/project/bursar/',
            },
            {
              label: 'npm',
              href: 'https://www.npmjs.com/package/@zonastery/bursar',
            },
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} bursar. GNU AGPL-3.0. Built with Docusaurus.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ['python', 'bash', 'json', 'yaml'],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
