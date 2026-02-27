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

## What's Been Implemented (Feb 2026)

### Sections
1. **Glassmorphism Navigation** — Logo (Playfair Display), smooth-scroll nav links (Our Lineage, Services, Process, FAQ), WhatsApp Scholar button, mobile hamburger menu
2. **Hero Section** — Full viewport split: Playfair Display headline ("20 Years in a Gurukul. One Sacred Space: Yours."), real havan fire macro photography, floating badge, dual CTAs, gold divider
3. **Trust Bar** — 3-column authority layer: 20+ Years Vedic Study | 10,000+ Shlokas | 100% Himalayan Samagri with gold separators
4. **Service Matrix (3 Premium Cards)** — Vastu Shanti Havan (₹15,000), Lakshmi-Kuber Havan (₹11,000), Maha Mrityunjaya (₹21,000) — each with card image, hover-lift, pricing, WhatsApp Inquire CTA
5. **Pedigree Section** — Deep maroon background, gold headline, scholar credentials (Gurukul trained Rishikesh, Rigveda/Atharvaveda, Sanskrit Phonetics)
6. **Process (3 Steps)** — 01 Muhurta Consultation | 02 Scholarly Preparation | 03 Vedic Execution
7. **FAQ Accordion** — 6 questions covering phonetic authenticity, pricing, Himalayan Samagri, booking process, service areas, customization
8. **Footer** — Dark (#150505), HomePujan in gold, tagline, navigation, WhatsApp CTA, phone number
9. **Floating WhatsApp Button** — Persistent bottom-right, pulse ring animation, links to wa.me/919667039964

### Technical Features
- Paper grain CSS texture overlay (via SVG filter)
- AOS (Animate On Scroll) with staggered fade-up entrances
- Smooth scrolling (CSS scroll-behavior)
- Navbar scroll shadow state
- Service card hover-lift with image zoom
- FAQ accordion (one-at-a-time, max-height transition)
- Mobile-first responsive at 375px+
- All interactive elements have `data-testid` attributes

## WhatsApp Integration
All CTAs link to: `https://wa.me/919667039964?text=I'm%20interested%20in%20booking%20a%20Vedic%20service.`
Phone: +91 96670 39964

## Test Results (Feb 2026)
- Frontend: 100% — All 14 test cases passed
- No broken images, no console errors

## Backlog / Potential Enhancements
- P1: Testimonials / client review section
- P1: Gallery of past ceremonies
- P2: Booking form / inquiry form (replace WhatsApp with embedded form + WhatsApp notification)
- P2: Service detail pages (individual Havan deep-dive pages)
- P3: SEO optimization (meta tags, schema markup for local business)
- P3: Multi-language support (Hindi)
- P3: Analytics integration (Google Analytics / Clarity heatmaps)
