# HomePujan — Ceremony Knowledge Wiki

A single, organized source of truth for every ceremony HomePujan offers.
One markdown note per ceremony. This wiki is what we use to build product
pages, write SEO articles, keep pricing consistent, and (later) power an
on-site AI helper.

This folder contains **no secrets** — it is safe to keep in the public repo
and doubles as an off-machine backup of HomePujan's ceremony knowledge.

## Folder layout

```
knowledge/
├── README.md                 # this file — how the wiki works
├── INDEX.md                  # list of all ceremonies + their status
└── ceremonies/
    ├── _TEMPLATE.md          # copy this to start a new ceremony note
    ├── rudrabhishek.md       # one note per ceremony
    └── ...
```

## How each note is structured

Every ceremony note has a small header (name, slug, category, price,
duration, **status**, sources) followed by plain sections: what it is, why
families choose it, who it's for, ritual steps, what's included, muhurat
guidance, pricing, FAQs, and a scholar to-verify checklist. Use
`_TEMPLATE.md` as the starting point so every note looks the same.

## The build-now, verify-later workflow

1. **Draft** — gather knowledge (Excel, existing site, reference sites) and
   write it up **in HomePujan's own words**. Never paste another site's text
   verbatim — it is a copyright risk and hurts SEO. Status starts as
   `draft`.
2. **Verify** — the scholar reviews and corrects. Tick the to-verify
   checklist, set status to `scholar-verified`, fill `last_verified`.
3. **Publish** — marketing copy can go live anytime; **ritual/factual
   details only go onto the live site once the note is `scholar-verified`.**

## The three things we do with the wiki

- **Ingest** — add new knowledge to a note (it grows over time, never
  re-gathered from scratch).
- **Use** — generate a product page, an SEO article, or an FAQ answer from a
  finished note.
- **Lint** — periodically check for gaps, stale prices, or contradictions.

## Status values

- `draft` — written, not yet checked by the scholar. Do **not** publish
  ritual/factual details.
- `scholar-verified` — checked and approved. Safe to publish.
