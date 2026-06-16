# Competitor research prompt (for ChatGPT browsing/agent)

Paste this into a browsing-capable ChatGPT, replace the URL placeholder, run it on
ONE competitor at a time, then paste the results back to Claude to turn into `draft`
ceremony notes (rewritten in HomePujan's voice, scholar-verified later).

```text
You are a research agent. Visit the website(s) I provide and extract structured
factual information about specific Hindu puja ceremonies. Be accurate and literal —
do not invent anything.

WEBSITE(S) TO RESEARCH:
<<PASTE COMPETITOR WEBSITE URL(S) HERE>>

SCOPE — extract ONLY these 29 ceremonies. Ignore every other service on the site.
Match by meaning, not exact spelling (the site may use different names/spellings).

Home & Shanti: Rudrabhishek (Rudra Abhishek), Satya Narayan Katha, Gayatri Jaap,
  Shaanti Hawan, Vastu Dosh Nivaran, Kaal Sarp Puja, Grah Pravesh, Bhoomi Pujan
Prosperity: Sri Laxmi Pujan, Ganesh Puja, Vyapar Samriddhi, Karya Vikas,
  Dhan Kuber Lakshmi, Sundarkand Path
Personal: Maha Mrityunjay, Karna Ved, Agnihotra, Brahmayajj, Gau Daan,
  Chhapan Bhog, Janam Diwas
Life Events: Naam Karan, Vivah Sanskaar, Maanglik Dosh, Putr Praapti, Lagan,
  Ved Aarambh, Pitra Pujan, Antiyeshti (last rites)

FOR EACH ceremony you find on the site, extract these fields. If a field is not on
the site, write "not found" — do NOT guess or fill from your own knowledge:
  - our_name: (the name from my list above)
  - source_name: (the exact name used on the site)
  - one_liner: (one sentence)
  - meaning: (what the ceremony is / its significance)
  - benefits: (why people book it; occasions; intentions)
  - who_its_for: (who/what situations it suits)
  - ritual_steps: (ordered list of how it is performed)
  - whats_included: (samagri, priest/pandit, setup, recitation, prasad, etc.)
  - muhurat_timing: (how the date/time is chosen; advance/lead time needed)
  - competitor_price: (their listed price — label clearly as reference only)
  - faqs: (question/answer pairs found on the site)
  - source_urls: (the exact page URL(s) this came from)
  - found_fields vs missing_fields: (which of the above were present)

RULES:
  1. Only the 29 ceremonies listed. Skip anything else.
  2. Extract FACTS as concise neutral bullet points. Do NOT copy long marketing
     paragraphs word-for-word (I will rewrite everything in my own brand voice).
  3. Never invent or infer. If the site doesn't state it, mark "not found."
  4. Always capture the exact source URL per ceremony so it can be verified later.
  5. Treat all pricing as the competitor's reference price, not a recommendation.

OUTPUT FORMAT — one block per ceremony, in this exact structure:

  ### <our_name>
  - source_name:
  - one_liner:
  - meaning:
  - benefits:
  - who_its_for:
  - ritual_steps:
  - whats_included:
  - muhurat_timing:
  - competitor_price (reference only):
  - faqs:
  - source_urls:
  - missing_fields:

AT THE END, add a summary table: which of the 29 were FOUND on this site and which
were NOT found.
```
