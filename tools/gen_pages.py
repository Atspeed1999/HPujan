#!/usr/bin/env python3
"""
gen_pages.py — generate per-ceremony product pages for HomePujan.

Wiki-first: each page is DERIVED from its knowledge note
(knowledge/ceremonies/<slug>.md), which is the single source of truth.
The verified satya-narayan-katha.html is the template; per-ceremony body
sections + head metadata are rebuilt from the note, while the shared chrome
(nav / trust / footer / booking dialogs / CSS / scripts) is reused verbatim.

Run from repo root:  python3 tools/gen_pages.py
Emits HTML into frontend/public/.  Does NOT touch services.html or sitemap.xml
(that wiring is a separate, reviewed step).
"""
import os, re, html, sys, json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUB  = os.path.join(ROOT, "frontend", "public")
NOTES = os.path.join(ROOT, "knowledge", "ceremonies")
TEMPLATE = os.path.join(PUB, "satya-narayan-katha.html")

# backend slug -> (output filename, short duration for hero)
FILEMAP = {
    "grahpravesh":  ("grah-pravesh.html",        "~1–2 hours"),
    "bhoomipujan":  ("bhoomi-pujan.html",         "~1.5–2 hours"),
    "gayatri":      ("gayatri-jaap.html",         "~1.5 hours"),
    "shaanti":      ("shaanti-hawan.html",        "~2 hours"),
    "vastu":        ("vastu-dosh-nivaran.html",   "~1.5 hours"),
    "kaalsarp":     ("kaal-sarp-puja.html",       "~4 hours"),
    "laxmi":        ("sri-laxmi-pujan.html",      "~1–2 hours"),
    "ganesh":       ("ganesh-puja.html",          "~45 min–1 hour"),
    "vyapar":       ("vyapar-samriddhi.html",     "~1.5 hours"),
    "karyavikas":   ("karya-vikas.html",          "~1.5 hours"),
    "kuberlakshmi": ("dhan-kuber-lakshmi.html",   "~2 hours"),
    "sundarkand":   ("sundarkand-path.html",      "~3 hours"),
    "mrityunjay":   ("maha-mrityunjay.html",      "~4 hours"),
    "karnavedh":    ("karna-ved.html",            "~1–1.5 hours"),
    "agnihotra":    ("agnihotra.html",            "~30–45 min"),
    "brahmayajj":   ("brahmayajj.html",           "~1.5–2 hours"),
    "gaudaan":      ("gau-daan.html",             "~1–2 hours"),
    "chhapanbhog":  ("chhapan-bhog.html",         "~1.5–2 hours"),
    "janamdiwas":   ("janam-diwas.html",          "~1.5 hours"),
    "namkaran":     ("naam-karan.html",           "~1.5 hours"),
    "vivah":        ("vivah-sanskaar.html",       "~5 hours"),
    "maanglik":     ("maanglik-dosh.html",        "~2 hours"),
    "putrpraapti":  ("putr-praapti.html",         "~2 hours"),
    "lagan":        ("lagan.html",                "~1–1.5 hours"),
    "vedaarambh":   ("ved-aarambh.html",          "~1.5 hours"),
    "pitrapujan":   ("pitra-pujan.html",          "~1–2 hours"),
    "antiyeshti":   ("antiyeshti.html",           "~5 hours"),
}

# ---------------------------------------------------------------- helpers
def esc(s):
    return html.escape(s, quote=False).replace("₹", "&#8377;")

def jstr(s):  # safe inside a single-quoted JS string in an onclick attr
    return s.replace("\\", "\\\\").replace("'", "\\'").replace('"', "&quot;")

def collapse(s):
    return re.sub(r"\s+", " ", s).strip()

# Markers of INTERNAL (scholar/ops-facing) content that must never reach a
# customer page. Whole sentences containing any of these are dropped.
INTERNAL = re.compile(
    r"competitor|for HomePujan|HomePujan to decide|HomePujan'?s offering|drafted from|"
    r"open question|reference only|scholar/ops|to confirm|should confirm|scholar should|"
    r"distinguished from|almost certainly|packaging|positioned for|their price|"
    r"key open question|must clarify|must be made transparent|^Note:",
    re.I)

def md_clean(s):
    """Strip markdown emphasis and drop *(editorial asides)*; return plain text."""
    s = re.sub(r"\*\([^)]*\)\*", "", s)          # *(To confirm with HomePujan)*
    s = s.replace("**", "")                        # bold markers
    s = re.sub(r"(?<![\w*])\*(.+?)\*(?![\w*])", r"\1", s)  # *italics*
    s = collapse(s)
    s = re.sub(r"^[\s—–-]+", "", s)                # leading dash left by aside removal
    return collapse(s)

def scrub(s):
    """Remove sentences that contain internal/competitor commentary, then clean."""
    s = re.sub(r"^>\s?", "", s, flags=re.M)        # strip blockquote markers
    s = re.sub(r"\*\([^)]*\)\*", "", s)            # drop inline asides BEFORE filtering
    sents = re.split(r"(?<=[.!?])\s+", collapse(s))
    kept = [x for x in sents if x.strip() and not INTERNAL.search(x)]
    out = md_clean(" ".join(kept))
    return out[:1].upper() + out[1:] if out else out

def rupee(n):
    return "&#8377;{:,}".format(int(n))

# ---------------------------------------------------------------- note parsing
def parse_note(path):
    txt = open(path, encoding="utf-8").read()
    parts = txt.split("---", 2)
    fm_raw, body = parts[1], parts[2]
    fm = {}
    for line in fm_raw.splitlines():
        m = re.match(r"^([a-z_]+):\s*(.*)$", line)
        if m:
            fm[m.group(1)] = m.group(2).strip()
    sections = {}
    cur = None
    for line in body.splitlines():
        m = re.match(r"^##\s+(.*)$", line)
        if m:
            cur = m.group(1).strip()
            sections[cur] = []
        elif cur is not None:
            sections[cur].append(line)
    sections = {k: "\n".join(v).strip() for k, v in sections.items()}
    return fm, sections

def sec(sections, prefix):
    pl = prefix.lower()
    for k, v in sections.items():
        if k.lower().startswith(pl):
            return v
    return ""

def bullets(text):
    out = []
    for line in text.splitlines():
        m = re.match(r"^\s*-\s+(.*)$", line)
        if m:
            out.append(collapse(m.group(1)))
    # merge wrapped bullet continuations is unnecessary; notes keep bullets short
    return out

def numbered(text):
    """Parse '1. **Title** — desc' style steps -> [(title, desc)]."""
    out = []
    # join wrapped lines: a new item starts with digit-dot
    items = re.split(r"\n(?=\s*\d+\.\s)", text.strip())
    for it in items:
        it = collapse(re.sub(r"^\s*\d+\.\s*", "", it))
        if not it:
            continue
        m = re.match(r"\*\*(.+?)\*\*\s*[—\-:]*\s*(.*)$", it)
        if m:
            out.append((collapse(m.group(1)), collapse(m.group(2))))
        else:
            # no bold lead: use first few words as title
            words = it.split()
            out.append((" ".join(words[:4]), it))
    return out

def parse_samagri(text):
    """Return dict with optional 'puja','havan','you' lists + 'note'."""
    res = {"puja": [], "havan": [], "you": [], "note": ""}
    # blockquote note
    qm = re.findall(r"^>\s?(.*)$", text, flags=re.M)
    if qm:
        res["note"] = collapse(" ".join(qm))
    # bold-labelled blocks:  **Label:** items...
    blocks = re.findall(r"\*\*([^*]+?):\*\*\s*(.*?)(?=\n\*\*[^*]+?:\*\*|\n>|\Z)",
                        text, flags=re.S)
    def items_of(raw):
        raw = md_clean(raw)
        raw = re.sub(r"^the standard puja kit\s*[—\-:]*\s*", "", raw, flags=re.I)
        raw = raw.rstrip(". ")
        parts = []
        for p in re.split(r"[,;]", raw):
            p = re.sub(r"^\s*(and|&)\s+", "", p.strip(" ."), flags=re.I).strip(" .")
            if p:
                parts.append(p)
        return parts
    for label, raw in blocks:
        ll = label.lower()
        its = items_of(raw)
        if "havan" in ll:
            res["havan"] = its
        elif "family" in ll or "you provide" in ll or "you" == ll.strip():
            res["you"] = its
        elif "puja" in ll or "homepujan" in ll or "provided" in ll:
            if not res["puja"]:
                res["puja"] = its
        else:
            # non-standard label (e.g. gaudaan "the cow itself & donation")
            res.setdefault("extra", []).append((collapse(label), collapse(re.sub(r"\*\*","",raw))))
    return res

def parse_faqs(text):
    pairs = re.findall(r"\*\*Q:\*\*\s*(.*?)\n\*\*A:\*\*\s*(.*?)(?=\n\*\*Q:\*\*|\Z)",
                       text, flags=re.S)
    return [(collapse(q), collapse(a)) for q, a in pairs]

# ---------------------------------------------------------------- card wiring
def parse_cards(services_html):
    cards = {}
    blocks = re.split(r'(?=<div class="lib-card")', services_html)
    for b in blocks:
        m = re.search(r'data-testid="card-([^"]+)"', b)
        if not m:
            continue
        slug = m.group(1)
        img = re.search(r'<img src="([^"]+)"', b)
        cat = re.search(r'lib-cat-badge">([^<]*)<', b)
        ben = re.search(r'lib-card-benefit">([^<]*)<', b)
        cards[slug] = {
            "img": img.group(1) if img else "",
            "cat": html.unescape(cat.group(1).strip()) if cat else "",
            "benefit": html.unescape(ben.group(1).strip()) if ben else "",
        }
    return cards

# ---------------------------------------------------------------- section builders
def build_head(d):
    L = []
    L.append('  <title>{} at Home — Puja by Gurukul Scholars | HomePujan</title>'.format(esc(d["name"])))
    L.append('  <meta name="description" content="{}"/>'.format(esc(d["meta_desc"])))
    L.append('  <link rel="canonical" href="https://homepujan.com/{}"/>'.format(d["clean"]))
    L.append('  <meta name="theme-color" content="#4A0E0E"/>')
    L.append('  <link rel="icon" type="image/png" href="https://homepujan.com/images/logo.png"/>')
    L.append('  <link rel="apple-touch-icon" href="https://homepujan.com/images/logo.png"/>')
    L.append('  <meta property="og:type" content="product"/>')
    L.append('  <meta property="og:site_name" content="HomePujan"/>')
    L.append('  <meta property="og:title" content="{} at Home — Puja by Gurukul Scholars"/>'.format(esc(d["name"])))
    L.append('  <meta property="og:description" content="{}"/>'.format(esc(d["og_desc"])))
    L.append('  <meta property="og:url" content="https://homepujan.com/{}"/>'.format(d["clean"]))
    L.append('  <meta property="og:image" content="{}"/>'.format(d["img_abs"]))
    L.append('  <meta name="twitter:card" content="summary_large_image"/>')
    L.append('  <meta name="twitter:title" content="{} at Home — by Gurukul Scholars | HomePujan"/>'.format(esc(d["name"])))
    L.append('  <meta name="twitter:image" content="{}"/>'.format(d["img_abs"]))
    L.append('  <link rel="preconnect" href="https://fonts.googleapis.com"/>')
    L.append('  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>')
    L.append('  <link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@400;600;700;900&family=Playfair+Display:ital,wght@0,400;0,600;0,700;1,400&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet"/>')
    L.append('  <link rel="stylesheet" href="booking.css"/>')
    # JSON-LD: Service
    service_ld = {
        "@context": "https://schema.org", "@type": "Service",
        "serviceType": d["name"], "name": d["name"] + " at Home",
        "description": d["ld_desc"],
        "provider": {"@type": "LocalBusiness", "name": "HomePujan",
                     "url": "https://homepujan.com/", "telephone": "+91-9667039964",
                     "areaServed": "IN"},
        "areaServed": "IN",
        "offers": {"@type": "Offer", "price": str(d["price"]), "priceCurrency": "INR",
                   "availability": "https://schema.org/InStock",
                   "url": "https://homepujan.com/" + d["clean"]},
    }
    L.append('  <script type="application/ld+json">')
    L.append('  ' + json.dumps(service_ld, ensure_ascii=False))
    L.append('  </script>')
    # JSON-LD: FAQ
    faq_ld = {"@context": "https://schema.org", "@type": "FAQPage",
              "mainEntity": [{"@type": "Question", "name": q,
                              "acceptedAnswer": {"@type": "Answer", "text": a}}
                             for q, a in d["faqs"]]}
    L.append('  <script type="application/ld+json">')
    L.append('  ' + json.dumps(faq_ld, ensure_ascii=False))
    L.append('  </script>')
    # JSON-LD: Breadcrumb
    bc = {"@context": "https://schema.org", "@type": "BreadcrumbList",
          "itemListElement": [
              {"@type": "ListItem", "position": 1, "name": "Home", "item": "https://homepujan.com/"},
              {"@type": "ListItem", "position": 2, "name": "The Vedic Library", "item": "https://homepujan.com/services.html"},
              {"@type": "ListItem", "position": 3, "name": d["name"], "item": "https://homepujan.com/" + d["clean"]},
          ]}
    L.append('  <script type="application/ld+json">')
    L.append('  ' + json.dumps(bc, ensure_ascii=False))
    L.append('  </script>')
    return "\n".join(L) + "\n"

def build_hero(d):
    return '''  <header class="hero"><div class="wrap">
    <div>
      <div class="eyebrow">{eyebrow}</div>
      <h1 class="serif">{name}, performed at your home</h1>
      <div class="sub">{sub}</div>
      <p class="lead">{lead}</p>
      <div class="cta">
        <a class="btn btn-gold" href="#" onclick="openPayModal('{slug}');return false;">Book &amp; Pay &middot; Pick Date &amp; Time</a>
        <a class="btn btn-ghost" href="#" onclick="openConsultModal('{cn}');return false;">Free Consultation</a>
      </div>
      <div class="price">Dakshina <b>from {price}</b> &nbsp;&middot;&nbsp; {dur} &nbsp;&middot;&nbsp; all Samagri included</div>
    </div>
    <div class="hero-img">
      <img src="{img}" alt="{alt}" onerror="this.onerror=null;this.src='https://homepujan.com/images/logo.png';this.style.objectFit='contain';this.style.background='#FFFEFB';"/>
      <span class="badge">Performed by Certified Gurukul Scholars</span>
    </div>
  </div></header>

  '''.format(eyebrow=esc(d["eyebrow"]), name=esc(d["name"]), sub=esc(d["sub"]),
             lead=esc(d["lead"]), slug=d["slug"], cn=jstr(d["name"]),
             price=rupee(d["price"]), dur=esc(d["dur"]), img=d["img_abs"],
             alt=esc(d["name"] + " — " + d["sub"]))

def build_what(d):
    cards = []
    cards.append('      <div class="card"><h3>Why families choose it</h3><p>{}</p></div>'.format(esc(d["why"])))
    if d["timing"]:
        cards.append('      <div class="card"><h3>When it’s performed</h3><p>{}</p></div>'.format(esc(d["timing"])))
    return '''  <section class="block"><div class="wrap">
    <div class="kicker">What is {name}</div>
    <h2 class="title">{h2}</h2>
    <p class="lede">{lede}</p>
    <div class="grid2">
{cards}
    </div>
  </div></section>

  '''.format(name=esc(d["name"]), h2=esc(d["what_h2"]), lede=esc(d["what_lede"]),
             cards="\n".join(cards))

def build_included(d):
    lis = "\n".join('      <li>{}</li>'.format(esc(x)) for x in d["included"])
    return '''  <section class="block" style="background:var(--cream)"><div class="wrap">
    <div class="kicker">What's included</div>
    <h2 class="title">One transparent dakshina. Nothing hidden.</h2>
    <ul class="incl">
{lis}
    </ul>
  </div></section>

  '''.format(lis=lis)

def build_samagri(d):
    s = d["samagri"]
    cards = []
    if s["puja"]:
        cards.append(_sam_card("Puja Samagri &mdash; we bring", s["puja"]))
    # NOTE: 'extra' (non-standard) samagri blocks are deliberately NOT rendered —
    # in the notes they carry internal scholar/ops caveats, not customer content.
    if s["havan"]:
        cards.append(_sam_card("Havan Samagri &mdash; if the havan form is chosen", s["havan"]))
    if s["you"]:
        cards.append(_sam_card("You provide at home", s["you"], you=True))
    if not cards:
        return ""  # no samagri section for this ceremony
    # Always use the standard, customer-safe note (blockquote notes are internal).
    note = "Samagri may vary slightly with the scale of your Sankalpa and seasonal availability. Anything specific is always confirmed during your free consultation."
    # Keep the grid from looking empty when a ceremony has fewer than 3 cards.
    grid_style = ""
    if len(cards) == 1:
        grid_style = ' style="grid-template-columns:1fr;max-width:440px"'
    elif len(cards) == 2:
        grid_style = ' style="grid-template-columns:1fr 1fr;max-width:720px"'
    return '''  <section class="block"><div class="wrap">
    <div class="kicker">What we bring &amp; what you provide</div>
    <h2 class="title">Every item of Samagri, accounted for</h2>
    <p class="lede">Our scholar arrives with all the puja Samagri. You only arrange a few household items and a clean space — nothing to source or worry about.</p>
    <div class="sam-grid"{gs}>
{cards}
    </div>
    <p class="sam-note">{note}</p>
  </div></section>

  '''.format(cards="\n".join(cards), note=esc(note), gs=grid_style)

def _sam_card(title, items, you=False):
    lis = "\n".join('          <li>{}</li>'.format(esc(x)) for x in items)
    cls = " you" if you else ""
    return '''      <div class="sam-card{cls}">
        <h3>{title}</h3>
        <ul>
{lis}
        </ul>
      </div>'''.format(cls=cls, title=title, lis=lis)

def build_steps(d):
    steps = d["steps"]
    if not steps:
        return ""
    cells = "\n".join(
        '      <div class="step"><h4>{}</h4><p>{}</p></div>'.format(esc(t), esc(p))
        for t, p in steps)
    return '''  <section class="block"><div class="wrap">
    <div class="kicker">How a {name} unfolds</div>
    <h2 class="title">From Sankalpa to Aarti</h2>
    <div class="steps">
{cells}
    </div>
  </div></section>

  '''.format(name=esc(d["name"]), cells=cells)

def build_pricing(d):
    return '''  <section class="pricing"><div class="wrap">
    <div>
      <div class="kicker">Book your {name}</div>
      <h2 class="title serif">Begin with a free consultation</h2>
      <p class="lede">Every booking starts with a short conversation — we understand your Sankalpa, recommend the right Muhurta (auspicious timing), and confirm the details. No obligation.</p>
    </div>
    <div class="pcard">
      <div class="from">Dakshina from</div>
      <div class="amt">{price}</div>
      <small>{dur} &middot; all Samagri included</small>
      <a class="btn btn-gold" href="#" onclick="openPayModal('{slug}');return false;">Book &amp; Pay &middot; Pick Date &amp; Time</a>
      <a class="btn" href="#" onclick="openConsultModal('{cn}');return false;" style="background:var(--maroon);color:var(--cream);margin-top:10px">Book Free Consultation</a>
      <div class="note">Final dakshina depends on Sankalpa, scale &amp; travel. Confirmed before you commit.</div>
    </div>
  </div></section>

  '''.format(name=esc(d["name"]), price=rupee(d["price"]), dur=esc(d["dur"]),
             slug=d["slug"], cn=jstr(d["name"]))

def build_faq(d):
    rows = "\n".join(
        '    <details><summary>{}</summary><div class="a">{}</div></details>'.format(esc(q), esc(a))
        for q, a in d["faqs"])
    return '''  <section class="block"><div class="wrap">
    <div class="kicker">Questions</div>
    <h2 class="title">Good to know</h2>
{rows}
  </div></section>

  '''.format(rows=rows)

def build_final(d):
    return '''  <section class="final">
    <h2 class="serif">{tagline}</h2>
    <p>Speak with a Gurukul scholar — no cost, no obligation.</p>
    <div class="cta">
      <a class="btn btn-gold" href="#" onclick="openPayModal('{slug}');return false;">Book &amp; Pay &middot; Pick Date &amp; Time</a>
      <a class="btn btn-ghost" href="#" onclick="openConsultModal('{cn}');return false;">Free Consultation</a>
      <a class="btn btn-ghost" href="https://homepujan.com/services.html">Explore all ceremonies</a>
    </div>
  </section>

  '''.format(tagline=esc(d["final_tagline"]), slug=d["slug"], cn=jstr(d["name"]))

# ---------------------------------------------------------------- assembly
def replace_between(s, start, end, new):
    i = s.index(start) + len(start)
    j = s.index(end, i)
    return s[:i] + "\n" + new + s[j:]

def build_page(base, d):
    out = base
    # head metadata + JSON-LD (everything from <title> up to <style>)
    h0 = out.index("  <title>")
    h1 = out.index("  <style>")
    out = out[:h0] + build_head(d) + out[h1:]
    # body sections
    out = replace_between(out, "<!-- HERO -->", "<!-- TRUST -->", build_hero(d))
    out = replace_between(out, "<!-- WHAT -->", "<!-- INCLUDED -->", build_what(d))
    out = replace_between(out, "<!-- INCLUDED -->", "<!-- SAMAGRI -->", build_included(d))
    out = replace_between(out, "<!-- SAMAGRI -->", "<!-- RITUAL STEPS -->", build_samagri(d))
    out = replace_between(out, "<!-- RITUAL STEPS -->", "<!-- PRICING -->", build_steps(d))
    out = replace_between(out, "<!-- PRICING -->", "<!-- FAQ -->", build_pricing(d))
    out = replace_between(out, "<!-- FAQ -->", "<!-- FINAL CTA -->", build_faq(d))
    out = replace_between(out, "<!-- FINAL CTA -->",
                          "<!-- ===================== FOOTER (site-wide) ===================== -->",
                          build_final(d))
    # shared-chrome scalar replaces (nav x2, footer x1, mobile bar)
    out = out.replace("openConsultModal('Satya Narayan Katha')",
                      "openConsultModal('{}')".format(jstr(d["name"])))
    out = out.replace("openPayModal('satyanarayan')", "openPayModal('{}')".format(d["slug"]))
    # Clean URLs: drop .html from internal links to the Vedic Library.
    out = out.replace("homepujan.com/services.html", "homepujan.com/services")
    # serviceData
    sd = "window.serviceData = {{ {slug}: {{ name: \"{name}\", subtitle: \"{sub}\", priceInr: {price} }} }};".format(
        slug=d["slug"], name=d["name"].replace('"', '\\"'),
        sub=d["sub"].replace('"', '\\"'), price=int(d["price"]))
    out = re.sub(r"window\.serviceData = \{.*?\};", lambda m: sd, out, count=1, flags=re.S)
    return out

def first_sentences(text, n=2):
    text = collapse(text)
    parts = re.split(r"(?<=[.!?])\s+", text)
    return " ".join(parts[:n]).strip()

def make_data(slug, fm, sections, card):
    file, dur = FILEMAP[slug]
    name = fm.get("name", slug).strip()
    price = int(re.sub(r"[^\d]", "", fm.get("price_inr", "0")) or 0)
    benefit = card.get("benefit", "").strip()
    img = card.get("img", "")
    img_abs = img if img.startswith("http") else "https://homepujan.com" + img

    oneliner = scrub(sec(sections, "One-liner"))
    whatis = scrub(sec(sections, "What it is"))
    why_bullets = [md_clean(b) for b in bullets(sec(sections, "Why families choose"))
                   if not INTERNAL.search(b)]
    timing = first_sentences(scrub(sec(sections, "Muhurat")), 2)
    included = [md_clean(b) for b in
                (bullets(sec(sections, "What's included")) or bullets(sec(sections, "What’s included")))]
    samagri = parse_samagri(sec(sections, "Samagri"))
    steps = [(md_clean(t), scrub(p)) for t, p in numbered(sec(sections, "Ritual steps"))[:6]]
    faqs = [(md_clean(q), scrub(a)) for q, a in parse_faqs(sec(sections, "FAQ"))]
    faqs = [(q, a) for q, a in faqs if q and a]   # drop FAQs emptied by scrubbing

    # why-card prose from benefit bullets
    why = "; ".join(b.rstrip(".").lstrip("To ").lstrip("to ") for b in why_bullets[:3])
    if why:
        why = "Families perform it to " + why[0].lower() + why[1:] + "."
    else:
        why = oneliner
    # always-present dakshina FAQ (price-aware), appended if not already covered
    faqs = list(faqs)
    if not any("dakshina" in q.lower() or "price" in q.lower() or "cost" in q.lower() for q, _ in faqs):
        faqs.append((
            "What does the dakshina depend on?",
            "It starts from ₹{:,} and varies with the scale of the ceremony, your specific Sankalpa, and the scholar's travel. The exact amount is always confirmed during your free consultation — never any hidden fees.".format(price),
        ))

    meta_desc = "Book {} at home — {} From ₹{:,}, all Samagri included, performed by Gurukul scholars.".format(
        name, first_sentences(oneliner, 1).rstrip("."), price)
    meta_desc = collapse(meta_desc)[:300]
    og_desc = collapse(oneliner)
    ld_desc = first_sentences(whatis, 2) or oneliner

    return {
        "slug": slug, "file": file, "clean": file[:-5] if file.endswith(".html") else file,
        "name": name, "price": price, "dur": dur,
        "img_abs": img_abs,
        "eyebrow": "{} · {}".format(name, benefit) if benefit else name,
        "sub": benefit or "Vedic Ceremony at Home",
        "lead": oneliner,
        "meta_desc": meta_desc, "og_desc": og_desc, "ld_desc": ld_desc,
        "what_h2": "What {} is, and why it's performed".format(name),
        "what_lede": whatis or oneliner,
        "why": why, "timing": timing,
        "included": included or [
            "A certified Gurukul scholar, travelling to your home",
            "All premium Samagri for the puja",
            "Aarti and post-ritual prasad",
            "A 15-minute pre-ritual consultation",
        ],
        "samagri": samagri, "steps": steps, "faqs": faqs,
        "final_tagline": "Invite divine grace into your home with {}".format(name),
    }

def main():
    base = open(TEMPLATE, encoding="utf-8").read()
    cards = parse_cards(open(os.path.join(PUB, "services.html"), encoding="utf-8").read())
    # map note files by frontmatter slug
    notes = {}
    for fn in os.listdir(NOTES):
        if not fn.endswith(".md") or fn.startswith("_") or fn == "INDEX.md":
            continue
        fm, sections = parse_note(os.path.join(NOTES, fn))
        if "slug" in fm:
            notes[fm["slug"]] = (fm, sections)
    report = []
    for slug in FILEMAP:
        if slug not in notes:
            report.append((slug, "MISSING NOTE", ""))
            continue
        if slug not in cards:
            report.append((slug, "MISSING CARD", ""))
            continue
        fm, sections = notes[slug]
        d = make_data(slug, fm, sections, cards[slug])
        page = build_page(base, d)
        open(os.path.join(PUB, d["file"]), "w", encoding="utf-8").write(page)
        flags = []
        if not d["samagri"]["puja"]: flags.append("no-samagri")
        if not d["steps"]: flags.append("no-steps")
        if d["samagri"].get("extra"): flags.append("samagri-extra")
        if "unsplash" in d["img_abs"]: flags.append("unsplash-img")
        report.append((slug, d["file"], ",".join(flags)))
    print("Generated {} pages:".format(sum(1 for r in report if r[1].endswith('.html'))))
    for slug, file, flags in report:
        print("  {:14} -> {:26} {}".format(slug, file, flags))

if __name__ == "__main__":
    main()
