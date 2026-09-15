import { defineConfig } from 'vitepress'

export default defineConfig({
  title: 'rssd',
  description: 'A file-based RSS daemon. The filesystem is the API.',
  lang: 'en-US',
  base: '/rssd/',
  cleanUrls: true,
  lastUpdated: true,

  head: [
    ['meta', { name: 'theme-color', content: '#d97757' }],
    ['meta', { property: 'og:title', content: 'rssd' }],
    ['meta', {
      property: 'og:description',
      content: 'A file-based RSS daemon. The filesystem is the API.',
    }],
  ],

  themeConfig: {
    nav: [
      { text: 'Guide', link: '/guide/getting-started' },
      { text: 'Reference', link: '/reference/cli' },
      { text: 'Spec', link: '/reference/spec' },
    ],

    sidebar: [
      {
        text: 'Guide',
        items: [
          { text: 'Getting started', link: '/guide/getting-started' },
          { text: 'How it works', link: '/guide/how-it-works' },
          { text: 'Entry format', link: '/guide/entries' },
          { text: 'Reading feeds', link: '/guide/reading' },
          { text: 'Events', link: '/guide/events' },
        ],
      },
      {
        text: 'Reference',
        items: [
          { text: 'Commands', link: '/reference/cli' },
          { text: 'Configuration', link: '/reference/config' },
          { text: 'Specification', link: '/reference/spec' },
        ],
      },
    ],

    socialLinks: [
      { icon: 'github', link: 'https://github.com/alliecatowo/rssd' },
    ],

    search: { provider: 'local' },

    editLink: {
      pattern: 'https://github.com/alliecatowo/rssd/edit/main/docs/:path',
      text: 'Edit this page on GitHub',
    },

    footer: {
      message: 'Released under the MIT License.',
      copyright: 'Copyright © 2026 Allie Coleman',
    },

    outline: [2, 3],
  },
})
