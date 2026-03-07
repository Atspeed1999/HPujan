# HomePujan.com - Product Requirements Document

## Original Problem Statement
Build and enhance the HomePujan.com website - a service for traditional Vedic ceremonies performed by Gurukul-trained scholars. The site should showcase services, allow users to book consultations, and provide an elegant, spiritual aesthetic matching "Apple + Phool" design philosophy.

## User Personas
1. **Seekers**: Individuals looking for Vedic ceremonies for life events (marriage, naming, etc.)
2. **Devotees**: Regular practitioners seeking prosperity, health, or peace rituals
3. **Business Owners**: Entrepreneurs seeking Vyapar Samriddhi or career advancement pujas
4. **Families**: Households wanting Vastu correction, ancestral peace, or ongoing spiritual support

## Architecture
```
/app/frontend/public/
├── landing.html     # Main landing page with hero, featured services, modals
└── services.html    # "Vedic Library" - complete list of 27 ceremonies
```

## Tech Stack
- **Frontend**: HTML5, Tailwind CSS (CDN), Vanilla JavaScript
- **Architecture**: Multi-page static site (no backend required)
- **Design**: "Parchment modal" aesthetic with maroon/gold color scheme

---

## Completed Features (December 2025)

### Phase 1 - Landing Page Foundation ✅
- [x] Hero section with "HomePujan" branding
- [x] Responsive navigation with dropdown menu
- [x] Featured services section (3 cards)
- [x] Process section explaining consultation flow
- [x] FAQ accordion section
- [x] Footer with WhatsApp contact

### Phase 2 - Interactive Modals ✅
- [x] Parchment-style modal design
- [x] Detailed service modals for featured ceremonies
- [x] "Vedic Alignment Call" consultation request form
- [x] WhatsApp integration for all contact flows

### Phase 3 - Services Library ✅
- [x] Created services.html ("The Vedic Library")
- [x] Sticky filter bar with category pills
- [x] Service cards with hover effects

### Phase 4 - Excel Data Integration (Current Session) ✅
- [x] **27 services from Excel file integrated**
  - Home & Shanti: Satya Narayan Katha, Gayatri Jaap, Shaanti Hawan, Vastu Dosh Nivaran, Kaal Sarp Puja, Rudraabhishek
  - Prosperity: Sri Laxmi Pujan, Ganesh Puja, Vyapar Samriddhi, Karya Vikas, Dhan Kuber Lakshmi, Sundarkand Path
  - Personal: Maha Mrityunjay, Karna Ved, Agnihotra, Brahmayajj, Gau Daan, Chhapan Bhog, Janam Diwas
  - Life Events: Naam Karan, Vivah Sanskaar, Maanglik Dosh, Putr Praapti, Lagan, Ved Aarambh, Pitra Pujan, Antiyeshti

- [x] **Service detail modals for all 27 ceremonies**
  - Structured content: Name, Subtitle, Category tags
  - THE VEDIC INTENT - Description section
  - THE SCHOLARLY EDGE - What makes the scholar special
  - THE ELEMENTS - Duration and Dakshina (price)
  - "Discuss with Jaynendra" WhatsApp button

- [x] **"View All 27 Vedic Ceremonies" button** on landing page
  - Placed under Sacred Ceremonies section
  - Links to services.html

- [x] **Live Chat floating button**
  - Fixed position (bottom-right)
  - Opens parchment modal with form
  - Fields: Name (required), Phone (required), Message (optional)
  - Sends to WhatsApp for offline follow-up

- [x] **Bug Fix: Dropdown menu hover timing**
  - Changed from display:none to opacity/visibility transitions
  - Added hover bridge (padding-top) on dropdown menu
  - Menu stays open when moving mouse from trigger to submenu

---

## Test Results
- **Frontend**: 100% pass rate
- **Filter counts verified**: 6+6+7+8 = 27 total ceremonies
- **All 27 Excel services confirmed integrated**
- **All modals and forms working correctly**

---

## Backlog / Future Tasks

### P1 - Enhancements
- [ ] Add more detailed descriptions from client (currently using placeholder scholarly content)
- [ ] Mobile optimization review
- [ ] SEO metadata enhancement
- [ ] Image optimization (compress/lazy load)

### P2 - New Features
- [ ] Testimonials section
- [ ] Blog/articles section
- [ ] Multi-language support (Hindi)
- [ ] Photo gallery of past ceremonies

### P3 - Analytics & Tracking
- [ ] Google Analytics integration
- [ ] Conversion tracking for WhatsApp clicks
- [ ] Heatmap integration (Clarity)

---

## Key Contacts
- **WhatsApp**: +91 96670 39964
- **Scholar**: Jaynendra (Gurukul-trained)

## Design System
- **Primary Color**: #4A0E0E (Maroon)
- **Accent Color**: #D4AF37 (Gold)
- **Background**: #FDFBF7 (Parchment)
- **Typography**: Cinzel (headings), Playfair Display (serif), Inter (body)
