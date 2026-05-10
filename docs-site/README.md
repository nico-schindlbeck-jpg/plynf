# Plinth Documentation Site

Static site for **plinth.dev**. Built with Astro + Tailwind. Zero external services, no analytics, no trackers.

## Develop locally

```bash
cd docs-site
npm install
npm run dev   # http://localhost:4321
```

## Build for production

```bash
npm run build      # Astro build + Pagefind index generation
npm run preview    # serve dist/ locally
```

The build runs `astro build` followed by `pagefind --site dist`, which generates a static full-text search
index under `dist/pagefind/`. Search is then served entirely from the static bundle — no external service
required.

## Deploy

Pushed to GitHub Pages on push to `main` via `.github/workflows/deploy-docs.yml`.

The workflow rebuilds whenever any of these change:

- `docs-site/**`
- `OVERVIEW.md`, `EXECUTIVE_SUMMARY.md`, `README.md`, `ARCHITECTURE.md`
- `docs/API_STABILITY.md`, `docs/compliance.md`, `docs/threat-model.md`, `docs/slos.md`, `docs/why-plinth.md`

Enable Pages in the repo Settings → Pages → Source: "GitHub Actions" before the first run.

## Adding content

1. Drop a Markdown or MDX file in `src/content/docs/`.
2. Add frontmatter — `title`, `description`, `section`, `order` (optional `sourceFile`).
3. Commit. The sidebar, the docs index, the search index, and the sitemap update automatically.

The content schema lives in `src/content/config.ts`. Section is one of:
`overview`, `guides`, `api`, `operations`.

```yaml
---
title: My new doc
description: One-line description shown in the docs index.
section: guides
order: 5
sourceFile: docs/my-source.md  # optional, shown as a chip on the page header
---
```

## Theme

Stone-toned palette + amber accent. Inter (body) + JetBrains Mono (code).
Tokens live in `tailwind.config.mjs`; prose styling and component classes live in `src/styles/globals.css`.

## Project structure

```
docs-site/
├── astro.config.mjs           # Astro config (Tailwind, MDX, sitemap)
├── tailwind.config.mjs        # design tokens
├── tsconfig.json
├── package.json
├── public/                    # static files served at /
│   ├── favicon.svg
│   ├── og-image.svg
│   └── robots.txt
└── src/
    ├── content/
    │   ├── config.ts          # content collection schema
    │   └── docs/              # all markdown sources
    ├── layouts/
    │   ├── BaseLayout.astro
    │   ├── HomeLayout.astro
    │   └── DocsLayout.astro
    ├── components/            # Hero, FeatureGrid, DemoComparison, SDKTabs, Nav, Footer, CodeBlock
    ├── pages/
    │   ├── index.astro        # landing
    │   ├── why.astro
    │   ├── pricing.astro
    │   ├── 404.astro
    │   └── docs/
    │       ├── index.astro    # docs landing + search box
    │       └── [...slug].astro
    └── styles/
        └── globals.css
```

## Performance budget

- Astro is zero-JS by default. The only JS shipped is for SDK-tab state, the demo bar animation, mobile nav,
  the TOC scroll-spy, and Pagefind's search runtime on the docs index. All of these are interactions, not
  hydration — total JS is well under 50 KB on every route.
- Custom prose styles avoid pulling `@tailwindcss/typography`.
- Fonts are bundled via `@fontsource/*` and self-hosted — no Google Fonts request.

## License

Apache 2.0 — same as the parent `plinth` repository.
