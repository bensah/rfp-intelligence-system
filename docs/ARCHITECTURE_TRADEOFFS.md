# Where to invest next — honest engineering assessment

You asked: is anything in the **Scrapy / Playwright / Crawlee / changedetection.io / Huginn / Firecrawl** ecosystem worth migrating to?

Short answer: **mostly no — but two of those tools (Playwright + LLM-assisted extraction) would significantly improve specific cases we currently can't handle.**

The rest is hype for our scale. Here's the breakdown.

---

## What our current stack actually does

```
[ config/sources.yaml + donor_sources DB ]
        ↓
[ ThreadPoolExecutor — 8 parallel workers ]
        ↓
[ core/scraper.py  ]   ← per-source dispatcher
   ├── RSS (feedparser)
   ├── REST JSON (Grants.gov)
   ├── HTML (requests + BeautifulSoup4 anchor extraction)
   └── Google Alerts (special-case RSS handler)
        ↓
[ _enrich_candidate ]  ← detail-page enrichment
   ├── PDF parsing (pypdf, 8-page cap)
   ├── PDF-guide following (Pierre Fabre pattern)
   ├── Deadline extraction (20+ regex patterns)
   └── Eligibility / description extraction
        ↓
[ core/auto_scorer.py ]
   ├── Search-URL gate
   ├── Language gate (Latin-script only)
   ├── Feasibility hard-reject
   ├── Deadline gate (explicit + URL-year fallback)
   ├── Country gate (inclusive-eligibility aware)
   ├── Theme gate
   └── CHAI decision tree
        ↓
[ core/scan_pipeline.py ]
   ├── find_duplicates (link / title-similarity / triple-key)
   ├── ingest (insert or merge)
   └── scan_logs row written
```

This is ~600 lines of focused Python. Not bad. Where does it break?

---

## Real limitations (in order of impact)

### 1. JavaScript-rendered pages — REAL problem
Sites where the listing widget is React/Vue and only renders after `window.onload`. Our BeautifulSoup gets the empty shell.

**Currently affected:**
- EC EU Funding Portal — full SPA
- CZI grants page
- Mastercard Foundation (some sections)
- DevelopmentAid grants aggregator

**Fix that would actually help: Playwright** (or its Python lib `playwright`).

### 2. LLM-quality extraction — REAL upside
Our regex-based extraction is fine for explicit phrases like "Deadline: April 10, 2026". It breaks when:
- The deadline is in a table row without a label
- "Applications open from 1 Jan to 31 Mar 2027" (two dates, no label)
- Eligibility hidden in a paragraph: "Open to non-profits in the WHO African region"
- CHAI role signals buried in a multi-paragraph "Who can apply" section
- Cross-language pages (we currently reject non-Latin instead of translating)

**Fix that would actually help: Claude API call per enriched candidate.** ~$0.001 per call with Haiku, ~$0.01 with Sonnet. For ~125 candidates per scan that's $0.10-1.20 per scan.

### 3. Page-change detection for "manual" sources — partly real
We have 14 sources marked `manual` (CZI, Mastercard, WHO ETDR, etc.) where humans should periodically check the page. We have no way to alert when those pages change.

**Possible fix:** changedetection.io, or simpler — store an HTML hash per scan and email when it changes.

---

## What WON'T help (despite the hype)

### Scrapy
Scrapy is excellent — for crawling thousands of sites with deep navigation, item pipelines, spider middleware, autothrottle, etc. Our 25 sources × shallow extraction don't justify the framework overhead. We'd spend a week rewriting working code into Scrapy's idioms with zero new capability. **Pass.**

### Crawlee
TypeScript/Node.js. Migrating our Python codebase to Node is a multi-week rewrite. Their Playwright integration is nicer than Scrapy's, but our scale doesn't justify a stack switch. **Pass.**

### Huginn
Ruby-based automation platform. Adds an entire service to operate. We can do the same with cron + Python in 50 lines. **Pass.**

### Firecrawl (cloud)
Paid service ($16-50/mo depending on volume). Returns Markdown for LLM consumption. Useful, but we'd be paying for clean text extraction we already get from BeautifulSoup + pypdf. The LLM extraction layer is separate from clean-Markdown generation — we can add the LLM directly without Firecrawl. **Pass for now.**

### Crawl4AI (open-source)
Same conceptual model as Firecrawl. Decent. Adds dependencies (it bundles Playwright + lxml + selectolax). If we're going to add Playwright anyway, we get most of Crawl4AI's value for free. **Pass; just use Playwright directly.**

---

## Recommended investments, in priority order

### Tier 1 — DO THESE NEXT (high value, ~1 day each)

**A. LLM-assisted extraction for the eligibility / deadline / criteria layer.**
- Adds an Anthropic API call per enriched candidate.
- The LLM receives the candidate text + a structured prompt and returns JSON: `{deadline, eligibility_summary, eligible_countries[], must_1_govt_alignment: Yes|Partial|No, ...}`.
- Replaces 5+ separate regex extractors with one structured call.
- Cost: ~$0.10-1.20 per scan with Haiku/Sonnet.
- Admin toggle: `Settings → Enable LLM enrichment`.
- Falls back to regex extraction when API quota exhausted / off.

**B. Playwright integration, behind a per-source feature flag.**
- Adds `method: html_js` to sources.yaml.
- When that method is used, scraper launches a headless Chromium, waits for `networkidle`, then hands the rendered HTML to the existing anchor extractor.
- Cost: +200MB dependency (Chromium binary), +2-3s per page.
- Worth it for the 4-5 currently-blocked SPA sources. Don't use for sources that work fine with `requests`.

### Tier 2 — Maybe later (medium value, ~2-3 days each)

**C. Change-detection for manual-only sources.**
- For every `method: manual` source, store an SHA256 of the page HTML weekly.
- When hash changes, email the team: "Mastercard Foundation page changed — review."
- Simpler than running changedetection.io as a separate service.

**D. More donor APIs.**
- We have Grants.gov (REST). World Bank, UN agencies, NIH, and several US federal donors expose similar APIs.
- Each API integration takes 1-2 hours and is more reliable than HTML scraping.
- Should research: AfDB e-procurement, UNDP procurement, NIH RePORTER for forecast-of-funding-opportunities.

### Tier 3 — Don't bother (low ROI)

- Scrapy / Crawlee migration
- Huginn / changedetection.io standalone service
- Firecrawl subscription
- "Self-improving" agents / autoscrapers

---

## The hard part isn't crawling

You said it yourself in the research: **"the hard part is normalising messy information."**

Our pipeline has 600 lines for that already. The remaining gaps:
- JS-rendered pages → Playwright
- Messy free-form text → LLM
- Page-change monitoring → SHA hash + email

These are 3 days of work, not a framework migration.

---

## Concrete next step

If you want to maximise impact-per-hour:

1. **Today**: enable LLM-assisted extraction (Tier 1A).
   - I'll add `core/llm_extractor.py` and the Admin toggle.
   - You add an `ANTHROPIC_API_KEY` to `.env`.
   - Result: substantially better deadline / eligibility / criteria detection.
2. **This week**: Playwright for the 4-5 SPA sources (Tier 1B).
   - Unblocks EC EU Portal, CZI, Mastercard, DevelopmentAid.
3. **Next week**: page-change hash for manual sources (Tier 2C).

Skip everything else.

If you want me to scaffold Tier 1A right now, say so and I'll wire it in behind a feature flag.
