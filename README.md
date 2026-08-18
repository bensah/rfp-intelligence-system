# RFPIS — RFP Intelligence System

A Streamlit + Supabase web app that automates RFP / call-for-proposals
discovery, eligibility scoring, decision tracking, and grant-pipeline
reporting for non-profit and research organizations. The deploying
org's profile (name, country, contact, logo) is configured in
**Admin → Settings → Organization**; everything else is org-agnostic.

> **Status:** v1.0 — foundation, scan pipeline, eligibility scoring,
> manual submission, decision tracking, KPI reporting all wired and
> shipping. See `docs/SCAN_PIPELINE.md` for the core scoring algorithm.

---

## Quick start (local)

```bash
python -m venv .venv
. .venv/Scripts/activate              # PowerShell / Git Bash on Windows
pip install -r requirements.txt
cp .env.example .env                  # then fill in SUPABASE_URL, SUPABASE_KEY, ...
streamlit run App.py
```

The app refuses to start until it can reach Supabase and find at least
one active user. See **One-time setup** below.

---

## One-time setup

### 1. Apply the schema to Supabase

Open your Supabase project → **SQL Editor** → New query, paste the
contents of `db/schema.sql`, run it.

A shortcut to copy the file to your clipboard (PowerShell):

```powershell
python scripts/apply_schema.py | Set-Clipboard
```

### 2. Seed the first admin user

```powershell
python scripts/seed_admin.py `
    --email you@yourdomain.org `
    --name "Your Name" `
    --password "a-strong-password"
```

Re-run safely; it upserts on email.

### 3. (Optional) Migrate historical RFP data from Excel

If you're moving off a spreadsheet-based screener, `scripts/migrate_excel.py`
will read an Excel workbook (path configured via `EXCEL_SOURCE_PATH` in
`.env`) and upsert rows into Supabase. Idempotent on
`rfp_submissions.uid` and `meeting_schedule.call_date`.

```powershell
python scripts/migrate_excel.py --dry-run   # preview row counts
python scripts/migrate_excel.py             # actually write to Supabase
```

### 4. Configure your organization

Sign in as the admin user, open **Admin → Settings → Organization** and
fill in your org name, country, contact email, and upload a logo. These
stamp the page header, the Report dashboard, and outbound email
digests.

### 5. (Optional) Install Playwright for SPA donor sources

A handful of donor portals (DevelopmentAid, EC Funding Portal, etc.)
render their listings via client-side JavaScript. Install Chromium so
the scanner can render them headlessly:

```powershell
playwright install chromium
```

The scanner falls back to a static-HTML scan if Playwright isn't
installed, so this step is optional.

---

## Repository layout

```
rfp-intelligence-system/
├── App.py                       # Streamlit entry + login gate
├── requirements.txt
├── .streamlit/config.toml        # green theme accent
├── .github/workflows/
│   └── weekly_scan.yml           # Friday cron — runs scripts/run_scan.py
│
├── assets/                       # RFPIS branding (placeholder SVGs)
├── auth/authenticator.py         # streamlit-authenticator + Supabase
├── db/
│   ├── schema.sql                # full Postgres schema
│   ├── migrations/               # idempotent ALTERs since launch
│   └── supabase_client.py
├── config/
│   ├── scoring_weights.yaml      # weights for the 9 MUST/PREFER criteria
│   ├── dropdowns.yaml            # controlled vocab — team names placeholders
│   ├── themes.yaml               # scraper search keywords
│   ├── sources.yaml              # donor sources the scanner crawls
│   └── donor_field_map.yaml      # per-donor field→column mapping
├── core/                         # scoring, scraping, settings, theme
├── pages/                        # one file per Streamlit page
├── views/                        # page bodies (rendered via render_view)
└── scripts/
    ├── apply_schema.py
    ├── seed_admin.py
    ├── migrate_excel.py
    └── run_scan.py               # CLI entry for the weekly cron
```

---

## Required secrets

| Variable             | Used by                                         |
|----------------------|-------------------------------------------------|
| `SUPABASE_URL`       | App + scanner                                   |
| `SUPABASE_KEY`       | App + scanner (service-role key)                |
| `APP_SECRET_KEY`     | streamlit-authenticator cookie signing          |
| `RESEND_API_KEY`     | Weekly digest email                             |
| `RESEND_FROM_EMAIL`  | Verified sender on your Resend domain           |
| `ADMIN_EMAIL`        | Scanner failure alerts                          |
| `ANTHROPIC_API_KEY`  | _Optional_ — LLM fallback for hard-to-parse RFPs |

Store them as Streamlit Community Cloud secrets and as GitHub Actions
secrets; mirror them in `.env` for local development. `.env` is
gitignored — never commit it.

---

## Deploying to Streamlit Community Cloud

1. Push the repo to GitHub (public — free tier requirement).
2. `share.streamlit.io` → New app → pick the repo → main branch →
   `App.py` as the entry.
3. App settings → Secrets — paste the same `.env` values.
4. Deploy. Note that `pip install playwright` does NOT fetch a browser, and
   the host does not run `playwright install` for you: the first PDF export
   downloads headless Chromium itself (~150MB, once per container, into a
   directory the app proves is writable). That first export therefore takes a
   couple of minutes; later ones are fast. Settings → Accounts → Deployment
   (or `?diag=1`) reports whether the browser is already present.

---

## License

TBD. Add a `LICENSE` file before sharing publicly.
