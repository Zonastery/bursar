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
  ],

  presets: [
    [
      'classic',
      {
        docs: {
          sidebarPath: './sidebars.ts',
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
    image: 'img/docusaurus-social-card.jpg',
    colorMode: {
      respectPrefersColorScheme: true,
    },
    navbar: {
      title: 'bursar',
      logo: {
        alt: 'bursar',
        src: 'img/logo.svg',
      },
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'docs',
          position: 'left',
          label: 'Docs',
        },
        {
          href: 'https://github.com/zonastery/bursar',
          label: 'GitHub',
          position: 'right',
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
              to: '/docs/intro',
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
      copyright: `Copyright © ${new Date().getFullYear()} bursar. GNU AGPL-3.0.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ['python', 'bash', 'json'],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
