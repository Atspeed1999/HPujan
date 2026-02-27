# HomePujan.com — Landing Page PRD

## Problem Statement
Build a high-end, single-page landing page for **HomePujan.com** — a brand that bridges ancient Vedic scholarship (20-year Gurukul lineage) with the expectations of Tier 1 urban professionals. Delivered as a **standalone HTML file** (Tailwind CDN + Vanilla JS), deployable on any standard server without a build process.

## Architecture

**Delivery Format:** Standalone HTML at `/app/frontend/public/landing.html`  
**Preview:** React App.js redirects `/` → `/landing.html` via `window.location.replace()`  
**Tech Stack:** HTML5, Tailwind CSS (CDN), Vanilla JS, Google Fonts CDN, AOS 2.3.4 CDN

## Visual Identity
- **Primary:** #4A0E0E (Deep Vedic Maroon)
- **Accent:** #D4AF37 (Antique Gold)
- **Background:** #FDFBF7 (Aged Parchment/Cream)
- **Headings:** Playfair Display (Serif)
- **Body:** Inter (Sans-serif)
- **Aesthetic:** Minimalist, scholarly, paper-grain texture overlay

## User Personas
- Tier 1 urban professionals (25–55 years), Delhi NCR and beyond
- Educated, affluent, spiritually inclined but discerning
- Seeking authentic, no-compromise Vedic rituals for their home

## What's Been Implemented (Feb 2026) — Final Version

### Sections
1. **Glassmorphism Navigation** — Cinzel logo, smooth-scroll nav links (Lineage, Services dropdown, Process, FAQ), WhatsApp Scholar button, mobile hamburger menu with auto-close
2. **Hero Section** — Full viewport split: Cinzel headline ("Ancient Precision. Modern Peace. 20 Years of Scholarly Mastery."), double-stroke antique gold frame on havan fire image, floating badge, dual CTAs ("Vedic Exchange" + "The Scholar's Lineage")
3. **Trust Bar** — 3-column authority layer: 20+ Years Vedic Study | 10,000+ Shlokas | 100% Himalayan Samagri
4. **Find Your Path (Diagnostic Funnel)** — 3 clickable tiles (New Beginnings/Home, Success & Growth/Business, Health & Protection/Wellness) with gold SVG icons, hover-lift, active state, WhatsApp footer link
5. **Vedic Services Filtered Grid** — 3 service cards with 400ms opacity-fade filter system; no "Shop/Buy" language — uses "Explore" and "Vedic Exchange"
6. **Parchment Modals (4-Section Structure)** — Each modal has deckle/rough paper edges (clip-path polygon), subtle grain overlay, and 4 scholarly sections:
   - **The Invocation**: Sanskrit name (Cinzel font) + Scholarly subtitle (Playfair italic)
   - **The Vedic Intent**: Spiritual problem the ceremony solves
   - **The Scholarly Edge**: Why 20-year mastery is specifically required (maroon left-border italic block)
   - **The Elements**: Duration, Scholar count, Samagri sourcing, Dakshina Estimate
   - "Discuss with Jaynendra" WhatsApp CTA button
7. **Scholarly Parampara (Lineage)** — Deep Maroon (#4A0E0E) background, 5% opacity Mandala watermark (concentric circles + lotus petals), "The Scholarly Parampara: A 20-Year Lineage" copy
8. **Sacred Process (3 Steps)** — Hand-drawn gold SVG icons: Sundial (Muhurta), Sacred Herbs (Preparation), Havan Kund Flame (Execution)
9. **FAQ Accordion** — 6 questions, one-at-a-time behavior, max-height transition
10. **Footer (Tactile Finish)** — Charcoal (#2D2D2D) with grainy SVG texture + gold top border
11. **Gold Lotus** — Floating scroll-to-top icon (bottom-left), appears after 350px scroll
12. **Floating WhatsApp** — Persistent bottom-right, pulse ring animation, wa.me/919667039964

### Technical Features
- CSS noise texture overlay (SVG fractalNoise, body::after)
- AOS 2.3.4 (scroll-triggered fade-up animations, staggered delays)
- Modal scrollable content (max-height: 92vh, overflow-y: auto with thin scrollbar)
- 400ms filter system: data-filter (tiles) / data-category (cards) attribute mapping
- Keyboard accessibility: Enter/Space on path tiles, Escape on modals
- Mobile-first responsive at 375px+
- All interactive elements have `data-testid` attributes
- Consultative language throughout: "Dakshina Estimate" (not "Investment/Pricing"), "Vedic Exchange" (not "Shop/Buy")

## Backlog / Potential Enhancements
- P1: Testimonials / client review section
- P1: Gallery of past ceremonies
- P2: Booking form / inquiry form (replace WhatsApp with embedded form + WhatsApp notification)
- P2: Service detail pages (individual Havan deep-dive pages)
- P3: SEO optimization (meta tags, schema markup for local business)
- P3: Multi-language support (Hindi)
- P3: Analytics integration (Google Analytics / Clarity heatmaps)
