import { defineConfig } from 'vitepress'

export default defineConfig({
  title: 'Ante',
  description: 'A retry-slot allocator for India\'s regulated e-mandate rails',
  lang: 'en-IN',

  // GitHub Pages project site: https://princegarg001.github.io/ante/
  base: '/ante/',
  cleanUrls: true,
  lastUpdated: true,

  head: [
    ['meta', { name: 'theme-color', content: '#b45309' }],
    ['meta', { property: 'og:type', content: 'website' }],
    ['meta', { property: 'og:title', content: 'Ante — a retry-slot allocator' }],
    [
      'meta',
      {
        property: 'og:description',
        content:
          'Three retries. Non-peak windows only. A 24-hour blind commitment. Every bet is collateralised by the mandate.',
      },
    ],
  ],

  markdown: {
    theme: { light: 'github-light', dark: 'github-dark' },
    lineNumbers: false,
  },

  themeConfig: {
    siteTitle: 'Ante',

    nav: [
      { text: 'Overview', link: '/guide/introduction' },
      { text: 'Constraints', link: '/constraints/' },
      { text: 'System', link: '/system/architecture' },
      { text: 'Engineering', link: '/engineering/verification' },
      { text: 'Results', link: '/analysis/results' },
      {
        text: 'Status',
        items: [
          { text: 'Roadmap', link: '/project/roadmap' },
          { text: 'Repository', link: 'https://github.com/princegarg001/ante' },
        ],
      },
    ],

    sidebar: [
      {
        text: 'Overview',
        collapsed: false,
        items: [
          { text: 'Introduction', link: '/guide/introduction' },
          { text: 'The problem', link: '/guide/problem' },
          { text: 'Prior art', link: '/guide/prior-art' },
        ],
      },
      {
        text: 'The regulatory spine',
        collapsed: false,
        items: [
          { text: 'Constraint register', link: '/constraints/' },
          { text: 'The three that matter', link: '/constraints/critical' },
          { text: 'Verification status', link: '/constraints/sources' },
        ],
      },
      {
        text: 'System',
        collapsed: false,
        items: [
          { text: 'Architecture', link: '/system/architecture' },
          { text: 'The world simulator', link: '/system/simulator' },
          { text: 'The allocator', link: '/system/allocator' },
          { text: 'Action space', link: '/system/action-space' },
          { text: 'The money path', link: '/system/action-layer' },
          { text: 'The edge', link: '/system/ingest' },
        ],
      },
      {
        text: 'Engineering',
        collapsed: false,
        items: [
          { text: 'Verification', link: '/engineering/verification' },
          { text: 'Mutation testing', link: '/engineering/mutation' },
          { text: 'Design decisions', link: '/engineering/decisions' },
        ],
      },
      {
        text: 'Analysis',
        collapsed: false,
        items: [
          { text: 'Results', link: '/analysis/results' },
          { text: 'Evaluation protocol', link: '/analysis/evaluation' },
          { text: 'Market data', link: '/analysis/market' },
        ],
      },
      {
        text: 'Project',
        collapsed: false,
        items: [{ text: 'Status & roadmap', link: '/project/roadmap' }],
      },
    ],

    socialLinks: [
      { icon: 'github', link: 'https://github.com/princegarg001/ante' },
    ],

    outline: { level: [2, 3], label: 'On this page' },

    editLink: {
      pattern: 'https://github.com/princegarg001/ante/edit/main/docs/:path',
      text: 'Edit this page on GitHub',
    },

    search: { provider: 'local' },

    footer: {
      message:
        'Track 03 · AI Revenue Recovery · Razorpay AI Buildathon. Every regulatory claim carries its source and verification status.',
      copyright: 'Built by princegarg001',
    },

    docFooter: { prev: 'Previous', next: 'Next' },
  },
})
