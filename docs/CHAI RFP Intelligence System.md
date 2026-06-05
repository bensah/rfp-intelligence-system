# CHAI Cameroon — RFP Intelligence System
## Complete Build Plan for Claude Code
**Version:** 1.0 | **Date:** June 2026 | **Owner:** Bernard Nsah (bensah @ GitHub)

---

## 1. Project Overview

The RFP Intelligence System (RFPIS) is a Streamlit-based web application that replaces the Excel-based CHAI Cameroon RFP Eligibility Screener. It automates weekly RFP discovery, scores opportunities against CHAI's nine eligibility criteria, enables manual submission by collaborators, and reproduces all existing dashboard views in a live, access-controlled web interface.

**Key principles:**
- One tool, no tool-switching (replaces Microsoft Forms + Excel + manual email digest)
- Automated Friday scan, human Monday review
- Duplicate detection and de-duplication at ingestion
- Access-controlled: CHAI Cameroon team only
- Free hosting via Streamlit Community Cloud, connected to GitHub user `bensah`
- Data stored in Supabase (free PostgreSQL tier) — no local files, no Excel dependency

---

## 2. Technology Stack

| Layer | Technology | Rationale |
|---|---|---|
| Frontend / UI | Streamlit | Direct Python-to-web, no frontend expertise needed, free hosting |
| Language | Python 3.11 | Single language across all layers |
| Database | Supabase (PostgreSQL) | Free tier, real-time, REST + Python SDK, accessible globally |
| Scheduler | GitHub Actions (cron) | Free, runs on schedule, no server needed |
| Authentication | Streamlit-Authenticator + Supabase auth | Role-based, invite-only |
| Email alerts | Resend (free tier, 3000 emails/day) | Weekly digest and submission alerts |
| Scraping | requests + BeautifulSoup + feedparser | Handles RSS, HTML pages, and JSON APIs |
| Scoring | Python (custom weighted scorer) | Mirrors the nine MUSTs/PREFERs from Criteria_Reference |
| Deployment | Streamlit Community Cloud | Free, connects directly to GitHub |
| Version control | GitHub (bensah account) | Source of truth; triggers deployments |

---

## 3. Repository Structure

```
chai-rfp-intelligence/
│
├── app.py                          # Main Streamlit entry point
├── requirements.txt                # Python dependencies
├── .streamlit/
│   └── config.toml                 # Theme and server settings
├── .github/
│   └── workflows/
│       └── weekly_scan.yml         # GitHub Actions — runs every Friday 06:00 WAT
│
├── auth/
│   └── authenticator.py            # Login, session management, role checks
│
├── pages/
│   ├── 01_RFP_Screening.py         # Mirrors RFP_Screening sheet (weekly dashboard)
│   ├── 02_RFP_Indepth_Review.py    # Mirrors RFP_In-depth_Review sheet
│   ├── 03_RFP_Tracking.py          # Mirrors RFP_Tracking sheet
│   ├── 04_RFP_Summary.py           # Mirrors RFP_Summary sheet (YTD)
│   ├── 05_Grants_Dashboard.py      # Mirrors Grants Dashboard sheet
│   ├── 06_Submit_RFP.py            # Manual submission form for collaborators
│   ├── 07_Meeting_Log.py           # Mirrors Meeting_Log sheet
│   ├── 08_Engagement_Log.py        # Mirrors Engagement_Log sheet
│   └── 09_Admin.py                 # Admin: user management, scan logs, overrides
│
├── core/
│   ├── scraper.py                  # All web scraping logic by source
│   ├── scorer.py                   # Eligibility scoring engine (9 criteria)
│   ├── deduplicator.py             # Duplicate detection logic
│   ├── notifier.py                 # Email digest via Resend
│   └── uid_generator.py            # Generates Form_ID (e.g., BE-260202-1220)
│
├── db/
│   ├── supabase_client.py          # Supabase connection singleton
│   ├── schema.sql                  # Full database schema (run once)
│   └── migrations/                 # Future schema changes
│
├── config/
│   ├── sources.yaml                # List of RFP sources to scrape
│   ├── scoring_weights.yaml        # Weights for eligibility criteria
│   └── themes.yaml                 # Program areas, funder list, geographies
│
└── scripts/
    ├── run_scan.py                 # Entry point for the GitHub Actions scanner
    └── migrate_excel.py            # One-time import of existing Excel data
```

---

## 4. Database Schema (Supabase)

### Table: `rfp_submissions`
Replaces the main response table (Sheet1 / RFP_Screener_bak).

```sql
CREATE TABLE rfp_submissions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    uid             TEXT UNIQUE NOT NULL,           -- e.g. BE-260202-1220
    form_id         TEXT UNIQUE NOT NULL,           -- same as uid for now
    source          TEXT DEFAULT 'auto',            -- 'auto' or 'manual'
    submitted_by    TEXT,
    submitted_at    TIMESTAMPTZ DEFAULT now(),

    -- Opportunity description
    opportunity_id      TEXT,
    opportunity_title   TEXT NOT NULL,
    brief_description   TEXT,
    date_posted         DATE,
    funding_agency      TEXT,
    geographic_scope    TEXT[],                     -- array: ['Cameroon','Mali']
    program_area        TEXT[],
    focus_theme         TEXT,
    opportunity_link    TEXT,
    chai_role           TEXT,                       -- Prime / Sub / Technical
    funding_window      TEXT,                       -- Open / Rolling / One-off / Close
    submission_deadline DATE,
    expected_award_date DATE,
    time_to_award       TEXT,
    estimated_value     NUMERIC,
    currency            TEXT,
    project_duration    INTEGER,                    -- months
    submission_format   TEXT,

    -- Eligibility scoring (Yes / Partial / No / NULL)
    feasibility             TEXT,
    must_1_govt_alignment   TEXT,
    must_2_strategic_fit    TEXT,
    must_3_implementable    TEXT,
    must_4_compliant        TEXT,
    must_5_resourcing       TEXT,
    prefer_6_funding_quality TEXT,
    prefer_7_monitorable    TEXT,
    prefer_8_partnership    TEXT,
    prefer_9_scale          TEXT,
    decline_flags_present   BOOLEAN DEFAULT FALSE,
    key_risks               TEXT,
    alignment_score         NUMERIC,               -- 0-100, auto-computed

    -- Decision-making
    decision            TEXT,                      -- Proceed / Park / Decline
    decision_date       DATE,
    decision_rationale  TEXT,
    auto_recommendation TEXT,                      -- system suggestion before human review
    stage               TEXT,
    proposal_lead       TEXT,
    contributors        TEXT[],
    reviewers           TEXT[],
    support_roles       TEXT,
    progress_status     TEXT,
    amount_requested    NUMERIC,
    date_completed      DATE,
    donor_decision      TEXT,
    next_action         TEXT,
    assigned_to         TEXT,
    remarks             TEXT,
    action_deadline     DATE,
    last_update         DATE,
    date_of_approval    DATE,
    amount_secured      NUMERIC,
    currency_secured    TEXT,
    donor_program_officer TEXT,
    next_step           TEXT,
    kickoff_date        DATE,

    -- Audit
    review_week         TEXT,                      -- e.g. 'Week 22 (25 May - 31 May)'
    is_duplicate        BOOLEAN DEFAULT FALSE,
    duplicate_of_uid    TEXT,
    created_at          TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now()
);
```

### Table: `meeting_logs`
```sql
CREATE TABLE meeting_logs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    meeting_date    DATE NOT NULL,
    donor_title     TEXT,
    rfp_uid         TEXT REFERENCES rfp_submissions(uid),
    remarks         TEXT,
    actions         TEXT,
    owner           TEXT,
    deadline        DATE,
    created_at      TIMESTAMPTZ DEFAULT now()
);
```

### Table: `engagement_logs`
```sql
CREATE TABLE engagement_logs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    engagement_date DATE NOT NULL,
    donor           TEXT,
    engagement_type TEXT,
    format          TEXT,
    chai_lead       TEXT,
    donor_contacts  TEXT,
    purpose         TEXT,
    outcome         TEXT,
    linked_rfp_uid  TEXT REFERENCES rfp_submissions(uid),
    created_at      TIMESTAMPTZ DEFAULT now()
);
```

### Table: `active_grants`
```sql
CREATE TABLE active_grants (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    grant_id        TEXT NOT NULL,
    donor_title     TEXT,
    form_id_link    TEXT,
    award_date      DATE,
    end_date        DATE,
    report_type     TEXT,
    report_due_date DATE,
    submitted_date  DATE,
    status          TEXT,
    owner           TEXT,
    remarks         TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);
```

### Table: `users`
```sql
CREATE TABLE users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email       TEXT UNIQUE NOT NULL,
    name        TEXT,
    role        TEXT DEFAULT 'collaborator', -- 'admin', 'reviewer', 'collaborator'
    is_active   BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT now()
);
```

### Table: `scan_logs`
```sql
CREATE TABLE scan_logs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scan_date       TIMESTAMPTZ DEFAULT now(),
    source          TEXT,
    rfps_found      INTEGER,
    rfps_new        INTEGER,
    rfps_duplicate  INTEGER,
    errors          TEXT,
    duration_sec    NUMERIC
);
```

---

## 5. RFP Sources to Scan

The scanner covers health-relevant sources across bilateral, multilateral, private foundation, and challenge fund categories. No restriction to a predefined donor list — the keyword filter handles relevance.

### Priority Sources (RSS or structured APIs)
| Source | URL / API | Method |
|---|---|---|
| UNGM (WHO, UNDP, UNICEF, UNFPA) | ungm.org/Public/Notice | HTML scrape with filters |
| Grants.gov | api.grants.gov/v2 | REST API (JSON) |
| NIH Guide | grants.nih.gov/grants/guide/rss/ | RSS |
| Wellcome Trust | wellcome.org/research-funding | HTML scrape |
| BMGF / Grand Challenges | gcgh.grandchallenges.org | HTML scrape |
| Global Fund | theglobalfund.org/en/funding-model/ | HTML scrape |
| Unitaid | unitaid.org/calls-for-proposals | HTML scrape |
| Gavi | gavi.org/about-us/work-us/rfps-eois | HTML scrape |
| FundsForNGOs | fundsforngos.org | RSS |
| DevEx | devex.com/funding | RSS (free tier) |
| GiveWell | givewell.org/research | HTML scrape |
| World Bank Projects | search.worldbank.org/api | REST API |
| European Commission (EU4Health, EDCTP) | ec.europa.eu/info/funding | HTML scrape |
| Google.org Impact Challenges | google.org/impact-challenges | HTML scrape |
| MIT Solve | solve.mit.edu/challenges | HTML scrape |
| Pivot/ProQuest | pivot.proquest.com | HTML scrape (authenticated) |
| ReliefWeb (humanitarian) | reliefweb.int/jobs/funding | RSS |
| CIHR | cihr-irsc.gc.ca/e/funding | HTML scrape |
| Fogarty / NIH LMICs | grants.nih.gov/funding/searchGuide | HTML scrape |

### Search Keywords (applied across all sources)
Grouped by domain to maximise recall:

```yaml
geographic:
  - Cameroon, Mali, Nigeria, Senegal, Côte d'Ivoire
  - West Africa, Central Africa, WCA, sub-Saharan Africa, LMICs

thematic:
  - malaria, HIV, tuberculosis, TB, NTDs, neglected tropical diseases
  - maternal health, MNCH, reproductive health, family planning
  - vaccines, immunisation, immunization, Gavi
  - cancer, NCDs, non-communicable diseases, diabetes, hypertension
  - digital health, mHealth, health information systems, eHealth, AI health
  - health financing, health systems strengthening, UHC
  - pandemic preparedness, One Health, AMR, antimicrobial resistance
  - nutrition, stunting, wasting, SAM
  - climate and health, climate adaptation
  - community health workers, CHW, frontline health workers
  - evidence synthesis, implementation research, health data

organisation_type:
  - technical assistance, implementation support, capacity building
  - NGO, nonprofit, non-profit, INGO eligible
```

---

## 6. Scoring Engine

The engine mirrors the Excel Criteria_Reference sheet exactly, with one added field: `alignment_score` (0–100).

### Scoring Logic

Each of the nine criteria is scored as: `Yes = 1.0`, `Partial = 0.5`, `No = 0.0`, `NULL = 0.0 (unscored)`.

Weights (configurable in `config/scoring_weights.yaml`):

```yaml
must_1_govt_alignment:    0.15   # High — country programme anchor
must_2_strategic_fit:     0.15   # High — SCALE/INNOVATE/FINANCE/PREPARE
must_3_implementable:     0.15   # High — CHAI execution advantage
must_4_compliant:         0.10   # Medium — compliance gate
must_5_resourcing:        0.10   # Medium — feasibility
prefer_6_funding_quality: 0.08   # Preferred
prefer_7_monitorable:     0.08   # Preferred
prefer_8_partnership:     0.10   # Preferred — consortium
prefer_9_scale:           0.09   # Preferred — sustainability
```

**Auto-recommendation logic:**

```
If decline_flags_present = True  → auto_recommend = Decline
Elif alignment_score >= 70       → auto_recommend = Proceed
Elif alignment_score >= 45       → auto_recommend = Park
Else                             → auto_recommend = Decline
```

Human reviewers can override on Mondays. Override is logged with rationale.

### Duplicate Detection

Two RFPs are flagged as duplicates if ANY of the following match:
1. Identical `opportunity_link` (exact URL)
2. Title similarity >= 90% (using `difflib.SequenceMatcher`)
3. Same `funding_agency` + `submission_deadline` + `estimated_value` combination

When a duplicate is detected, the newer submission is marked `is_duplicate = True` and linked to the original via `duplicate_of_uid`. It is excluded from all dashboards but visible in an admin audit view.

---

## 7. Application Pages

### Page 1: RFP Screening (Weekly Dashboard)
Mirrors the `RFP_Screening` sheet. Shows the current week's intake:

- KPI row: Total Screened / Proceed / Parked / Declined counts
- Duplicate alert banner (if any)
- Proceed RFP list with inline decision override dropdown
- Largest opportunity and nearest deadline callouts
- Prime vs. Sub opportunity counts
- Filter by: decision, feasibility, geography, program area

### Page 2: RFP In-depth Review
Mirrors `RFP_In-depth_Review`. One-at-a-time detailed card view:

- All key details from the submission
- Eligibility criteria visualised as a badge grid (✓ / ◐ / ✗)
- Alignment score gauge
- Decision override with rationale text box
- Key risks, auto-recommendation vs. confirmed decision
- Navigation: previous/next opportunity within the current week

### Page 3: RFP Tracking (Proceed Pipeline)
Mirrors `RFP_Tracking`. Shows all "Proceed" RFPs with pipeline management:

- Days to deadline, deadline status (On Track / Due Soon / Overdue)
- Stage selector (Identification → Submission → Post-submission)
- Progress status, team assignments
- Next action log with owner and deadline
- Meeting log entries linked to each RFP

### Page 4: YTD Summary Dashboard
Mirrors `RFP_Summary`. Year-to-date aggregated view:

- Screening snapshot: total, proceed, parked, declined, duplicates
- Pipeline health: stage breakdown, probability tiers, pipeline value (USD)
- Proposal development status: not started, in progress, submitted, discontinued
- Action urgency table: overdue, due soon, on track
- Forward funding coverage
- Donor concentration (top three by share)
- KR2.2 / KR2.3 / KR2.4 metrics (donor engagements, missed deadlines, narrative status)
- Annual reflection panel

### Page 5: Grants Dashboard
Mirrors `Grants Dashboard`. Tracks submitted and approved grants only:

- Key metrics: total active, total requested (USD), pending decision, secured, win rate
- By funding agency breakdown table
- Per-grant drill-down with team, progress, meeting log history

### Page 6: Submit RFP (Manual Form)
Replaces Microsoft Forms for collaborators who find RFPs outside the automated scan.

Form sections:
1. **Opportunity Description:** Title, Funder, Geography (multi-select), Program Area (multi-select), Link, Brief Description, Date Posted, CHAI Role, Window, Deadline, Award Date, Value, Currency, Duration
2. **Eligibility Scoring:** Nine MUST/PREFER dropdowns (Yes / Partial / No)
3. **Decline Flags:** Yes / No + key risks text
4. **Initial Decision:** Proceed / Park / Decline (submitter's recommendation)
5. **Team:** Submitted by (auto-filled from login), Proposal Lead, Contributors

On submission: duplicate check runs immediately. If duplicate detected, submitter sees a warning with the original UID and can confirm or cancel.

### Page 7: Meeting Log
Mirrors `Meeting_Log`. Real-time collaborative note capture during Monday BDT calls. Features: week selector (defaults to current week), RFP selector (filtered to Proceed RFPs only), live entry form with fields for Remarks, Actions/Recommendations, Owner (dropdown of team members), and Deadline. Save writes instantly to Supabase. Read view shows all entries for the selected week grouped by RFP, with outstanding actions highlighted. Carries forward unresolved actions to the next week automatically. Note-taker role shown at top (pulled from the Schedule sheet data).

- Add new meeting entry linked to an active RFP
- Filter by donor, date range, owner
- Export to CSV for offline distribution

### Page 8: Engagement Log
Mirrors `Engagement_Log`. Donor relationship tracker feeding KR2.2. Features: log form for Engagement Date, Donor, Engagement Type (Meeting / Call / Conference / Email / Pitch), Format (In-person / Virtual), CHAI Lead, Donor Contact(s), Purpose, Outcome and Follow-up, and optional link to an active RFP. Summary panel shows quarterly count against the target of two to four engagements, with a status indicator (below target / on track / exceeding). Separate from meeting logs intentionally — a donor engagement may have no linked RFP (e.g., a relationship-building call at a conference):

- Log meetings, calls, conferences
- Link to active RFP (optional)
- Quarterly count with target indicator

### Page 9: Admin Panel (admin role only)
- User management: invite users, set roles, deactivate
- Scan logs: history of all automated scans, errors, RFPs found per source
- Manual scan trigger: run scanner on demand
- Duplicate audit: review and resolve duplicate flags
- Scoring weights editor: adjust criteria weights without code changes
- Data export: full CSV export of all tables

---

## 8. Authentication and Access Control

### Roles
| Role | Access |
|---|---|
| `admin` | All pages including Admin Panel |
| `reviewer` | All pages except Admin Panel; can confirm decisions |
| `collaborator` | Submit RFP form, view Screening and Summary dashboards |

### Implementation
Use `streamlit-authenticator` library backed by the Supabase `users` table. On first deployment, the admin seeds the user list. New users are invited by email (Resend) and set their own password on first login.

Session tokens expire after 8 hours of inactivity. All decision overrides log the authenticated user's name and timestamp.

---

## 9. Automated Scanner (GitHub Actions)

### Schedule
Runs every Friday at 05:00 UTC (06:00 WAT, Cameroon time).

### Workflow File: `.github/workflows/weekly_scan.yml`

```yaml
name: Weekly RFP Scan

on:
  schedule:
    - cron: '0 5 * * 5'   # Every Friday 05:00 UTC
  workflow_dispatch:        # Allow manual trigger from GitHub Actions UI

jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - name: Install dependencies
        run: pip install -r requirements.txt
      - name: Run scanner
        env:
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_KEY: ${{ secrets.SUPABASE_KEY }}
          RESEND_API_KEY: ${{ secrets.RESEND_API_KEY }}
        run: python scripts/run_scan.py
```

### Scanner Steps (`scripts/run_scan.py`)
1. Load all configured sources from `config/sources.yaml`
2. For each source: fetch, parse, extract candidate RFPs
3. For each candidate: run duplicate check against existing database
4. For non-duplicates: run scoring engine, compute alignment_score and auto_recommendation
5. Write new records to `rfp_submissions` table in Supabase with `source = 'auto'` and `review_week` set to the upcoming Monday's week label
6. Write scan summary to `scan_logs` table
7. Send Friday digest email to all active subscribers via Resend

### Friday Email Digest Content
- Subject: `CHAI Cameroon | RFP Digest — Week [N] | [N] new opportunities`
- Body (HTML table):
  - New RFPs found this week, ranked by alignment_score descending
  - Columns: Title, Funder, Geography, Deadline, Value, CHAI Role, Alignment Score, URL
  - Reminder: Monday 09:00 review call

---

## 10. Build Phases

### Phase 1 – Foundation (Weeks 1–2)
Claude Code tasks:
1. Initialise repository at `bensah/chai-rfp-intelligence` with full folder structure
2. Write `db/schema.sql` and run against Supabase project
3. Write `db/supabase_client.py` singleton
4. Write `auth/authenticator.py` with role checks
5. Write `app.py` with Streamlit navigation, login gate, and sidebar
6. Write `scripts/migrate_excel.py` — one-time import of existing 44 RFP records from the Excel file into Supabase
7. Deploy skeleton to Streamlit Community Cloud, confirm login works

Deliverable: live URL with authenticated empty dashboard.

### Phase 2 – Core Screener Pages (Weeks 3–4)
Claude Code tasks:
1. Build Page 6 (Submit RFP form) — this is the most critical page; all other data flows from it
2. Build Page 1 (RFP Screening weekly dashboard)
3. Build Page 2 (RFP In-depth Review with decision override)
4. Write `core/scorer.py` and `core/deduplicator.py`
5. Write `core/uid_generator.py`

Deliverable: team can submit RFPs manually, score them, and confirm decisions.

### Phase 3 – Dashboards (Weeks 5–6)
Claude Code tasks:
1. Build Page 3 (RFP Tracking pipeline)
2. Build Page 4 (YTD Summary dashboard)
3. Build Page 5 (Grants Dashboard)
4. Build Page 7 (Meeting Log)
5. Build Page 8 (Engagement Log)

Deliverable: full dashboard parity with the Excel screener.

### Phase 4 – Automation (Weeks 7–8)
Claude Code tasks:
1. Write `core/scraper.py` with five priority sources (UNGM, Grants.gov RSS, FundsForNGOs RSS, BMGF Grand Challenges, Unitaid)
2. Write `scripts/run_scan.py`
3. Write `core/notifier.py` with Resend email template
4. Set up `.github/workflows/weekly_scan.yml`
5. Build Page 9 (Admin Panel)
6. Test full Friday scan to Monday review cycle end-to-end

Deliverable: first automated weekly digest sent to team.

### Phase 5 – Expansion and Hardening (Weeks 9–10)
Claude Code tasks:
1. Add remaining scraper sources (10+ additional sources)
2. Tune scoring weights based on team feedback from first two Monday reviews
3. Add CSV export to all dashboard pages
4. Write unit tests for scorer.py and deduplicator.py
5. Add error monitoring (log to scan_logs; email admin on scan failure)
6. Write user documentation (README.md + one-page quick-start guide)

Deliverable: production-ready system.

---

## 11. Secrets and Environment Variables

Store all of the following as GitHub Actions secrets AND as Streamlit Community Cloud secrets:

```
SUPABASE_URL          — From Supabase project settings
SUPABASE_KEY          — Supabase service role key (server-side only)
RESEND_API_KEY      — From Resend account
APP_SECRET_KEY        — Random string for Streamlit session signing
ADMIN_EMAIL           — Ben's email for admin notifications
```

Never commit these to the repository. Use `.env` locally with `python-dotenv` for development.

---

## 12. Local Development Setup

Claude Code instructions to include in README:

```bash
# Clone repository
git clone https://github.com/bensah/chai-rfp-intelligence.git
cd chai-rfp-intelligence

# Create virtual environment
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Create local secrets file
cp .env.example .env
# Edit .env with your Supabase and Resend credentials

# Run the app locally
streamlit run app.py

# Run scanner manually (without sending emails)
python scripts/run_scan.py --dry-run
```

---

## 13. Deployment Steps (Streamlit Community Cloud)

1. Push repository to `github.com/bensah/chai-rfp-intelligence`
2. Go to `share.streamlit.io` → New app → Connect GitHub → select `bensah/chai-rfp-intelligence`
3. Set main file path to `app.py`
4. Add all secrets under "Advanced settings → Secrets"
5. Click Deploy — Streamlit assigns a URL (customisable to `chai-rfp.streamlit.app`)
6. Share URL with team; invite first users via Admin Panel

---

## 14. Requirements File

```
streamlit>=1.35.0
streamlit-authenticator>=0.3.3
supabase>=2.4.0
pandas>=2.2.0
numpy>=1.26.0
requests>=2.31.0
beautifulsoup4>=4.12.0
feedparser>=6.0.11
Resend>=6.11.0
python-dotenv>=1.0.0
pyyaml>=6.0.1
plotly>=5.22.0
difflib2>=1.0.0
openpyxl>=3.1.0
```

---

## 15. Claude Code Prompting Strategy

When working with Claude Code, give instructions phase by phase, file by file. Sample prompts:

**To initialise the project:**
> "Create the full folder structure for `chai-rfp-intelligence` as described in the build plan. Generate `requirements.txt`, `.streamlit/config.toml` with CHAI green branding (`#00703C` primary), and a placeholder `app.py` that requires login before showing any page."

**To build the database:**
> "Write `db/schema.sql` containing all six tables from the build plan. Then write `db/supabase_client.py` as a singleton that reads `SUPABASE_URL` and `SUPABASE_KEY` from environment variables and returns a connected client."

**To build the submission form:**
> "Write `pages/06_Submit_RFP.py` as a Streamlit page. It should render a three-section form matching the fields in the `rfp_submissions` table. On submit, it should call `core/deduplicator.py` first. If no duplicate, write to Supabase and show a success message with the generated UID. If duplicate, show a warning with the existing UID and ask the user to confirm before proceeding."

**To build the scorer:**
> "Write `core/scorer.py`. It takes a dict of nine MUST/PREFER values (each 'Yes', 'Partial', 'No', or None) and a boolean `decline_flags_present`. It reads weights from `config/scoring_weights.yaml`. It returns `alignment_score` (0–100) and `auto_recommendation` ('Proceed', 'Park', or 'Decline') using the logic in the build plan."

---

## 16. Key Decisions Summary

| Decision | Choice | Reason |
|---|---|---|
| Frontend | Streamlit | Fastest Python-to-web path; free hosting |
| Database | Supabase | Free PostgreSQL; real-time; Python SDK |
| Scheduler | GitHub Actions | Free cron; no infrastructure |
| Authentication | streamlit-authenticator + Supabase | Role-based; invite-only; no cost |
| Email | Resend free tier | Reliable; 100 emails/day covers team |
| Scope | WCA + LMICs + opportunistic | Donor diversification objective |
| Donor list | Open (keyword-filtered, not donor-filtered) | Avoid oversaturation by familiar donors |
| Hosting | Streamlit Community Cloud (bensah) | Free; CHAI-branded; public URL |
| Data migration | One-time script from existing Excel | Preserves all 44 existing records |
| Decision workflow | Auto-recommend Friday, human confirm Monday | Preserves existing team process |

---

*CHAI Cameroon — Business Development Team | Prepared for build with Claude Code*