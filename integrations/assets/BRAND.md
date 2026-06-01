# Plynf brand assets

Drop-in assets for marketplace listings (n8n, Zapier, Make, Copilot Studio) and
partner pages. All vector, so they scale to any size a store asks for.

## Files
| File | Use |
|------|-----|
| `plynf-mark.svg` | The mark on its own (nav, favicons, inline). |
| `plynf-icon-512.svg` | Square app icon (rounded, dark bg). Most stores want 256/512 PNG — export this at that size. |
| `plynf-banner-1280x640.svg` | Listing hero / OG banner. Export to PNG at 1280×640. |

Export SVG → PNG (most stores require PNG):
```bash
# with rsvg-convert (brew install librsvg) or any SVG tool / Figma
rsvg-convert -w 512 -h 512 plynf-icon-512.svg  > plynf-icon-512.png
rsvg-convert -w 1280 -h 640 plynf-banner-1280x640.svg > plynf-banner.png
```

## Colors
| Token | Hex | Use |
|-------|-----|-----|
| Coral / accent | `#ff8551` | primary accent, CTAs |
| Magenta | `#d946ef` | gradient mid |
| Indigo | `#6366f1` | gradient end, "cool" accent |
| Background | `#08070d` | near-black canvas |
| Ink | `#f8f7ff` / `#b9b5d4` / `#6e6a8a` | primary / secondary / muted text |

Primary gradient: `linear-gradient(135deg, #ff8551, #d946ef, #6366f1)`.

## Type
**Inter** (UI/headings) + **JetBrains Mono** (labels/code). Free, self-hostable.

## Screenshots for listings
Capture from the live dashboard (don't fake them):
1. **Overview** — lead with the savings hero (71.8% / €) + fleet status.
2. **Token Economics** — the with-vs-without chart and the water tally.
3. **Connect** (`/install`) — "pick what you use → one step".
Capture at 1280px wide, **dark theme**, on demo data.

## Voice (one-liners for store descriptions)
- "Cut agent token cost ~70% — and see the savings."
- "Point your base URL at Plynf. Same code, shaped + routed + audited."
- "The agent context optimization layer."

## Do / Don't
- **Do** keep clear space around the mark (≥ the height of one bar).
- **Do** put the mark on `#08070d` or white; keep the gradient intact.
- **Don't** recolor the gradient, stretch the mark, or add effects.
- **Don't** place third-party logos next to ours implying a partnership we
  don't have. Use names in text until a brand agreement is in place.
