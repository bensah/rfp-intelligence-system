# Extending the Scanner — Adding New Donor Sources

The scanner is intentionally org-agnostic. It extracts every candidate RFP it can find, then a separate, per-org policy layer filters which ones the team sees. To plug a new donor in, you only need to handle the **extraction** part — the eligibility / scoring is configured separately in [Admin → Settings → Scan eligibility & auto-scoring policies](../pages/05_Admin.py).

## The data dictionary — `config/donor_field_map.yaml`

Every donor exposes its own field vocabulary ("Estimated Total Program Funding" on Grants.gov, "Project value" on DevelopmentAid, etc.). **Before writing any extractor code, add the donor to `config/donor_field_map.yaml`.** That file is the single source of truth that translates each donor's vocabulary into the canonical columns of `rfp_submissions`. It exists because the scraper was historically growing ad-hoc per-donor parsers that silently drifted out of sync with the DB — the Grants.gov `oppId` → `opportunityId` bug (which silently dropped every detail field since launch) is the canonical example of what we're preventing.

The dictionary lists every canonical column at the top, then one section per donor describing endpoints, search keywords, and how each source-field maps to a canonical column. Read the existing `grants_gov` and `developmentaid` sections as templates — the YAML supports dotted paths, array indexers (`[*]` / `[0]`), and coercions (`as: money`, `as: date`, `as: html_to_text`).

Workflow when onboarding a new donor:

1. Open the donor's listing page and one detail page in a browser. Note every visible field that maps to something we care about (amount, deadline, eligibility, etc.).
2. Add a new top-level section to `donor_field_map.yaml` declaring those mappings — even if you haven't written the extractor yet.
3. Write the per-donor extractor in `core/scraper.py` (or a future `core/extractors/<donor>.py`) and have it execute the mapping.
4. Drop a smoke test that asserts the candidate has the fields the dictionary promised. If the donor adds or renames a field later, the smoke test fails and you know exactly what to update.

The dedup logic (`core/deduplicator.py`) reads its signals from the same dictionary's `dedup_signals` section. Currently four signals are wired: opportunity_id exact match (strongest, catches cross-source duplicates), URL match, title similarity ≥ 0.90, and the funder/deadline/value triple.

Every donor falls into one of three buckets. Pick the right bucket first; it's much cheaper than fighting the wrong tool.

---

## Bucket A — Static HTML listing page (easiest)

A page where the RFP / call titles are written directly into the initial HTML (right-click → View Source shows them). Our generic anchor extractor handles these.

**Test it in 30 seconds:**

```bash
curl -L "https://example-donor.org/funding/calls" | grep -i "call for\|RFP\|deadline" | head -10
```

If you see actual titles printed, this bucket works.

**Add to `config/sources.yaml`:**

```yaml
  - name: Example Donor
    method: html
    url: https://example-donor.org/funding/calls
    priority: 1     # 1 = high (active scan), 3 = low
```

**Test the integration:**

```bash
python scripts/run_scan.py --dry-run --source "Example Donor"
```

If `found > 0`, you're done. The Pierre Fabre source is the model — 18 candidates found on first run.

---

## Bucket B — JS-rendered page with a backend endpoint

The "main" listing page returns an empty HTML shell; React/Vue/jQuery fills it in afterwards by hitting a JSON or HTML-fragment endpoint. Browser dev tools reveal the endpoint.

**How to find the backend endpoint:**

1. Open the donor's RFP listing page in Chrome / Firefox.
2. F12 → **Network** tab.
3. Filter by **XHR** or **Fetch**.
4. Reload the page.
5. Scroll the network log for a request that returns the grant titles — usually `?action=…`, `/api/…`, or `admin-ajax.php`.
6. Right-click the request → **Copy → Copy URL** (or "Copy as cURL" to inspect headers).

**Example — Novo Nordisk Foundation:**

The visible page at `https://novonordiskfonden.dk/en/grant/` is empty until JS runs. The XHR call goes to:

```
https://novonordiskfonden.dk/wp/wp-admin/admin-ajax.php?action=content_api&type=grants&status=open&page=1&forced_lang=en&funding_area=&sort=DESC
```

This endpoint returns rendered HTML (the actual grant tiles). Our existing HTML scraper picks them up. We just point the source at the endpoint URL instead of the public page URL.

**Add to YAML:**

```yaml
  - name: Donor X Backend
    method: html      # endpoint returns HTML — keep method as html
    url: https://example.org/wp-admin/admin-ajax.php?action=grants&status=open
    priority: 1
```

If the endpoint returns **JSON** instead of HTML, see Bucket C.

---

## Bucket C — JSON REST API (best signal-to-noise)

Some donors expose proper REST APIs (Grants.gov, World Bank). Returns structured JSON — every field clean and labelled.

This requires a **per-donor handler** in `core/scraper.py`. Look at `_scan_grants_gov()` as the template:

```python
def _scan_grants_gov(name, url):
    for keyword in [...]:
        r = requests.post(url, json={...})
        for hit in r.json()["data"]["oppHits"]:
            yield {
                "opportunity_title": hit["title"],
                "opportunity_link":  f"https://www.grants.gov/.../{hit['id']}",
                "funding_agency":    hit["agencyName"],
                "submission_deadline": _parse_iso_date(hit["closeDate"]),
                ...
            }
```

Add the dispatcher line in `_scan_rest_json()`:

```python
def _scan_rest_json(name, url):
    if "api.grants.gov" in url:
        return _scan_grants_gov(name, url)
    if "your-new-api.org" in url:           # ← add here
        return _scan_your_new_donor(name, url)
    return []
```

Then declare the source:

```yaml
  - name: Your New Donor API
    method: rest_json
    url: https://your-new-api.org/v1/opportunities
    priority: 1
```

---

## Bucket D — Google Alerts (catch-all for everything else)

If a donor:

- Is anti-bot / 403s our requests (e.g. UNICEF Supply, WHO ETDR Salesforce)
- Is login-walled (e.g. EU Funding & Tenders Portal)
- Has no public listing page (e.g. Mastercard Foundation — invitation-only)

…the most reliable workaround is **Google Alerts**. Google indexes practically everything, and provides RSS feeds for arbitrary search queries.

**Setup (~2 min per alert):**

1. Go to https://www.google.com/alerts (sign in with the team Google account).
2. Type a search query in the "Create an alert about..." box. Examples:
   - `"call for proposals" "global health"`
   - `"request for proposals" "Cameroon" OR "Mali" health`
   - `"expression of interest" malaria OR TB OR HIV vaccine`
   - `site:foundation.org "open call" OR "RFP" 2026`
3. Click "Show options":
   - **How often**: As-it-happens (or At most once a day)
   - **Sources**: Automatic
   - **Language**: English
   - **Region**: Any region (or specific country)
   - **How many**: Only the best results
   - **Deliver to**: **RSS feed** ← critical
4. Click "Create Alert".
5. Back on the alert list, click the RSS icon next to your new alert. Copy the feed URL — it looks like:
   ```
   https://www.google.com/alerts/feeds/12345678901234567890/9876543210987654321
   ```
6. Paste into `config/sources.yaml`:
   ```yaml
     - name: Google Alert — Global Health RFPs
       method: rss
       url: https://www.google.com/alerts/feeds/.../...
       priority: 1
   ```

**Why this works:** Google's crawler hits donor sites we can't reach. New RFPs typically get indexed within hours. Our existing RSS scraper consumes the feed natively. The downside is some noise — a "call for proposals" search picks up news articles too. The eligibility gate filters most of that.

**Recommended alerts to seed:**

| Query | Catches |
|---|---|
| `"request for proposals" "global health"` | RFPs at major US/UK health foundations |
| `"call for proposals" Africa health 2026` | Africa-focused calls |
| `"expression of interest" "low-income countries"` | LMIC-targeted EOIs |
| `"funding opportunity" malaria OR tuberculosis OR HIV` | Disease-specific |
| `"notice of funding opportunity" NIH OR USAID` | US federal grants |
| `site:europa.eu "calls for proposals" health` | EU funding (works around the SPA) |

Each alert is a separate `source` entry. Keep them under `priority: 1` since they're the freshest signal.

---

## Maintenance — keeping URLs alive

Donors rename / redesign pages every 6-12 months. Use the validator:

```bash
python scripts/validate_donor_urls.py
```

Reports status per URL (OK / 404 / 403 / TLS / TIMEOUT). Run it before every quarterly review. Flip dead URLs to `method: manual` until you find the replacement, then back to `method: html`.

---

## Where the per-org filter lives

Once a source is extracting, the per-org eligibility filter (which RFPs the deploying org actually pursues) is configured entirely in the database via **Admin → Settings → Scan eligibility & auto-scoring policies**. The scanner doesn't know or care which org is using it. Same scanner, different policies → reusable for any non-profit with different country / theme / criterion priorities.
