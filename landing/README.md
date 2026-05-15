# plinth.dev — landing page

The marketing front for Plinth — the runtime where production AI agents actually work. Astro + Tailwind, fully static, dark-mode-first, designed to feel like the same studio that built linear.app and resend.com.

> This site is **separate** from `docs-site/` (which is documentation). This site is the thing that loads when someone tweets `plinth.dev`.

## Develop

```bash
cd landing
npm install
npm run dev      # http://localhost:4321
```

## Build & preview

```bash
npm run build    # static output to ./dist
npm run preview  # serve ./dist locally
```

## Deploy to Netlify

The repo ships with a `netlify.toml` — Netlify auto-detects everything. Two paths:

**One-shot CLI deploy:**

```bash
npm i -g netlify-cli       # one-time
netlify login              # one-time
netlify init               # one-time — link to a Netlify site
netlify deploy --prod      # ships /dist to plinth.dev
```

**Continuous deploy:** push to `main` and let Netlify auto-build via the `netlify.toml`. Recommended.

## Where things live

```
src/
├── pages/
│   ├── index.astro       # / — the marketing landing
│   ├── pricing.astro     # /pricing
│   ├── about.astro       # /about
│   ├── manifesto.astro   # /manifesto
│   └── 404.astro
├── layouts/
│   └── BaseLayout.astro  # html shell, fonts, meta, theme bootstrap
├── components/
│   ├── Nav.astro         # sticky top nav
│   ├── Hero.astro
│   ├── Logos.astro
│   ├── ProblemSolution.astro
│   ├── HowItWorks.astro
│   ├── FeatureGrid.astro
│   ├── CodeShowcase.astro  # 5-SDK tabbed code, Shiki-highlighted at build
│   ├── MetricsBar.astro
│   ├── Architecture.astro
│   ├── Pricing.astro
│   ├── FAQ.astro
│   ├── CTA.astro
│   ├── Footer.astro
│   └── ThemeToggle.astro
└── styles/
    └── globals.css        # design tokens, base styles, utilities
```

## Customization quick map

| Want to change… | Edit |
|---|---|
| Brand accent color | `--accent` in `src/styles/globals.css` |
| Hero copy | `src/components/Hero.astro` |
| Pricing tiers | `src/components/Pricing.astro` |
| FAQ items | `src/components/FAQ.astro` |
| Footer links | `src/components/Footer.astro` |
| Code examples (SDK tabs) | `src/components/CodeShowcase.astro` |
| Tailwind tokens | `tailwind.config.mjs` |
| Security headers / redirects | `netlify.toml` |

## Quality targets

- Lighthouse 95+ across Performance / Accessibility / Best Practices / SEO
- Initial HTML + CSS < 50 KB (gzipped)
- No client-side JavaScript framework — vanilla `<script>` for the few interactions
- Respects `prefers-reduced-motion`
- All interactives keyboard-accessible with visible focus rings

## License

Apache 2.0 — same as the Plinth runtime.
