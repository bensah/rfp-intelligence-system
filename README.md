# RFPIS — RFP Intelligence System

A Streamlit + Supabase web app that automates RFP / call-for-proposals
discovery, eligibility scoring, decision tracking, and grant-pipeline
reporting for non-profit and research organizations. The deploying org's
profile (name, country, contact) is configured in **Admin → Settings →
Organization**; everything below is org-agnostic.

> **Reference deployment:** CHAI Cameroon's Business Development Team
> (CHAI BDT), replacing their Excel-based RFP eligibility screener.
> The codebase ships with CHAI BDT as the default org profile so a
> fresh install isn't blank; change it in Admin → Settings.

> **Status:** Phase 1 (Foundation) — schema, auth, app shell, Excel migration.

---

## Quick start (local)

```bash
python -m venv .venv
.\.venv\Scripts\activate          # Windows PowerShell
pip install -r requirements.txt
copy .env.example .env             # then fill in SUPABASE_URL, SUPABASE_KEY, ...
streamlit run Home.py
```

The app refuses to start until it can reach Supabase and find at least one
active user. See **One-time setup** below.

---

## One-time setup

### 1. Apply the schema to Supabase

Open your Supabase project → **SQL Editor** → New query, then paste the
contents of `db/schema.sql` and run it.

A shortcut to copy the file to your clipboard:

```powershell
python scripts/apply_schema.py | Set-Clipboard
```

### 2. Seed the first admin user

```powershell
python scripts/seed_admin.py `
    --email you@clintonhealthaccess.org `
    --name "Your Name" `
    --password "a-strong-password"
```

Re-run safely; it upserts on email.

### 3. (Optional) Migrate the historical Excel data

```powershell
python scripts/migrate_excel.py --dry-run   # preview row counts
python scripts/migrate_excel.py             # actually write to Supabase
```

The migration is idempotent on `rfp_submissions.uid` and on
`meeting_schedule.call_date`. The remaining tables append.

---

## Phase 1 deliverable

`streamlit run Home.py` should:

1. show the login form
2. accept the admin credentials seeded in step 2
3. land on a welcome page with role + phase metrics in the sidebar
4. `Logout` clears the session

That's the foundation. Pages 1-9 are stubs that flip on in Phases 2-4.

---

## Repository layout

```
chai-rfp-intelligence/
├── Home.py                   # Streamlit entry + login gate
├── requirements.txt
├── .streamlit/config.toml    # CHAI green branding
├── .github/workflows/
│   └── weekly_scan.yml       # Friday 05:00 UTC cron (Phase 4)
│
├── auth/authenticator.py     # streamlit-authenticator backed by Supabase
├── db/
│   ├── schema.sql            # full Postgres schema (8 tables)
│   └── supabase_client.py    # singleton client
├── config/
│   ├── scoring_weights.yaml  # weights for the 9 criteria
│   ├── dropdowns.yaml        # controlled vocab from Excel
│   ├── themes.yaml           # scraper search keywords
│   └── sources.yaml          # scraper sources + out-of-scope donors
├── core/uid_generator.py     # BE-260202-1220 style UIDs
├── pages/                    # one file per app page (stubs in Phase 1)
└── scripts/
    ├── apply_schema.py       # dump schema.sql to stdout
    ├── seed_admin.py         # create the first admin user
    └── migrate_excel.py      # one-time historical import
```

---

## Required secrets

| Variable             | Used by                                         |
|----------------------|-------------------------------------------------|
| `SUPABASE_URL`       | App + scanner                                   |
| `SUPABASE_KEY`       | App + scanner (service-role key)                |
| `APP_SECRET_KEY`     | streamlit-authenticator cookie signing          |
| `RESEND_API_KEY`     | Phase 4 weekly digest                           |
| `RESEND_FROM_EMAIL`  | Verified sender on your Resend domain           |
| `ADMIN_EMAIL`        | Scanner failure alerts                          |

Store them as Streamlit Community Cloud secrets and as GitHub Actions secrets;
mirror them in `.env` for local development.

---

## Deviations from the original build plan

Captured during Excel inspection — folded into Phase 1:

- **`narrative_logs`** table added (KR2.4 — referenced by the YTD Summary plan
  but missing from the original schema).
- **`meeting_schedule`** table added (note-taker / presenter / chair rota
  referenced by the Meeting Log page).
- **`users.password_hash`** added (needed by streamlit-authenticator).
- **`rfp_submissions.search_date` / `notes`** added (Form1 metadata fields).
- **`config/dropdowns.yaml`** lists every controlled vocabulary used by the
  Excel form (feasibility, stages, decisions, engagement types, week labels,
  currency FX rates, etc.) so Phase 2's form pages can reuse them.
- **`out_of_scope_donors`** in `config/sources.yaml` — the 30+ donors the team
  has explicitly excluded; used as a negative filter by the Phase 4 scanner.

These keep behaviour parity with the Excel screener without changing the
plan's architecture.
