# Tenant Graph & Proxy Scoring — Design

Status: **Draft for build** · Owner: Bernard · Origin case: uid `BE-260831-1210`
(a US-federal APS where the active country-team tenant applied as a **Sub**).

> Naming note: this doc uses generic roles — **the child tenant** (the active
> country/team org), **the parent org**, **the Lead applicant**, **a co-Sub** — never
> real tenant/org identities, so it is safe to keep in the repo.

## 0. Why this exists

Today the scorer evaluates every criterion against **one** org profile — the
active tenant's (`core.org_profile.get_profile(tenant_id)`). That is wrong for
three real situations the origin case exposed all at once:

1. **The child tenant is a *Sub*, not the Prime.** The consortium is a Lead
   applicant plus two Subs (the child tenant + another co-Sub). Prime-facing
   eligibility (US registration, authorized signatory) is the *Prime's* burden, but
   the scorer charges it to the child tenant and auto-Declines.
2. **A parent org holds the credential.** The child tenant can sign for and receive
   US-federal money because its **parent org (US-registered)** holds UEI/SAM and the
   authorized signatory — a fact the child's own profile does not (and should not)
   carry.
3. **Relationships live across the family/consortium.** Past-grantee /
   donor-engaged / shared-collaborator (PREFER-7) and competitiveness track-record
   / portal-familiarity (PREFER-8) are satisfied by the parent or a co-applicant,
   not necessarily by the applying child alone.

The app **already stores** `applicant_role` (prime/sub), `lead_applicant`,
`sub_applicant` on every RFP (`views/submit_form.py:358`), and there is a robust
applicant-cell splitter (`core.partner_names.split_pieces`). **`criteria_derive`
just never consumes any of it.** This design closes that gap.

Decisions locked with owner (2026-08-31):
- Sub-applicant's own **Registration region** → **0.5 "unclear" (soft)**, never a
  hard 0, when the US-registration burden is the Prime's.
- Parent org modeled as a **parent tenant + link** (`tenants.parent_tenant_id`),
  consulted **only** for the components where a parent's standing legitimately
  transfers.
- Build the **tenant graph** as the primary lever (this doc), then the two
  isolated fixes ride on it.

---

## 1. Current seam (what changes plug into)

Scoring assembles a single `org` dict and threads it through everything:

- `views/review_rfp.py:175` → `_org_prof = _orgp.get_profile()`
- `views/review_rfp.py:201` → `derive_criteria(row, _org_prof, _donor_eff, _org_set)`
- `core/opportunity_scoring.py:104` → same, for the crawl/cron path
- `core/auto_scorer.py:2885` → same, for auto-scoring

All the component checks read only `org.get(...)`:
- Registration region — `criteria_derive.py:2119-2128` (`_region_covered`, `:1957`)
- Authorized signatory — `criteria_derive.py:1400-1406` (`_signatory_donor_match`, `:1184`)
- PREFER-7 tiers — `criteria_derive.py:2338-2379`
- PREFER-8 track record / portal — `criteria_derive.py:2496-2521`

**Strategy:** introduce an **effective-org resolver** that runs *upstream* of
`derive_criteria` and produces an `ApplicantGraph`. The graph is passed alongside
`org`; only the ~6 transfer-eligible checks consult it. `derive_criteria`'s
existing single-`org` contract stays intact for everything else, so blast radius
is contained and the non-transferable criteria are provably unchanged.

---

## 2. Data model

### 2.1 Parent link (migration)

Shipped as **migration 095** (`db/migrations/095_tenant_graph_parent_and_consortium.sql`)
— `parent_tenant_id uuid references tenants(id) on delete set null`, a
`tenants_parent_not_self` CHECK, and a `SECURITY DEFINER` cycle-guard trigger
(`trg_tenants_no_parent_cycle`) so no chain can loop. Columns only — **no rows, no
tenant names** (the file is committed to git; real identities never enter it).

**Creation & linking happen entirely in the UI, never by script:**
- A super_user creates a parent org the same way they create any tenant.
- An **admin** (inside their own tenant) can create a **child** tenant; it lands
  `status='pending'` and a super_user approves it — the **existing** tenant-request
  flow (migration 082), reused unchanged. No new approval mechanism.
- The link is **reassignable in any direction and at any time**: a child that already
  exists is simply pointed at a parent once that parent exists. The cycle-guard is the
  only restriction.
- The parent org is an ordinary tenant (`kind='organization'`) that may itself never
  run scans; it exists to hold the family's transferable standing.

### 2.2 Consortium data-sharing consent (migration)

Reading a **co-applicant tenant's** profile (the Lead, a co-Sub) for our scoring crosses
a real privacy boundary. Parent↔child is an ownership link (safe); co-applicants
are independent orgs. Gate it:

```sql
alter table tenants
  add column if not exists share_for_consortium_scoring boolean not null default false;
```

- Default **off** → a co-applicant tenant is *named but unresolved* for scoring.
- On → only a **whitelisted, non-sensitive subset** of their profile is readable by
  the scoring layer (see §7). Never the full profile, never in any tenant's UI.

### 2.3 Name → tenant resolution

`lead_applicant` / `sub_applicant` are free text. Resolve each name to a tenant via
`tenants.name`, `org_identity` name/short-name, plus an alias set. Reuse the existing
canonical matcher pattern (`_name_set` + ≥4-char substring, mirroring
`_canonical_donor_match`). No hit → unresolved (not an error).

---

## 3. The applicant graph

```
ApplicantGraph = {
  self:    profile,               # the active tenant (always present)
  parent:  profile | None,        # tenants.parent_tenant_id, whitelisted subset
  prime:   profile | None,        # resolved lead_applicant tenant, if any + consented
  cosubs:  [profile, ...],        # resolved sub_applicant tenants, consented
  role:    "prime" | "sub" | "",  # rfp.applicant_role
  unresolved_prime: bool,         # a Prime is named but not a resolvable/consented tenant
}
```

Built once per (rfp, tenant) by a new `core/applicant_graph.py`:

```python
def resolve(rfp, org, tenant_id) -> ApplicantGraph: ...
```

- `parent` via `parent_tenant_id` (service-client read, whitelisted fields, §7).
- `prime` / `cosubs` via `split_pieces(rfp["lead_applicant"/"sub_applicant"])` →
  name→tenant → consent check → whitelisted profile.
- Cached like `get_profile` (keyed on tenant + rfp id).

---

## 4. Transfer rules (the heart)

Each component declares a **transfer class**. A component may consult another
profile **only** in its class, in a fixed **precedence** (strongest own-standing
first, so we never downgrade a credential we actually hold).

| Component | Class | Consults (in order) | Rule |
|---|---|---|---|
| MUST-1 Registration region | **Consortium (prime-led)** | self → parent → prime | Prime/parent US-registered ⇒ **1**. Named-but-unresolved Prime ⇒ **0.5 soft**. Self US-registered ⇒ 1. Else 0.5 when we are a Sub; 0 when we are Prime and not registered. |
| MUST-1 HQ country (item C) | **Consortium** | self → prime | For an HQ-restricted call where we are a Sub, the Prime's HQ satisfies it. (Phase 2 — low frequency.) |
| MUST-5 Authorized signatory | **Parent + consortium** | self → parent → prime → cosubs | Any consulted profile lists this donor in `org_authorized_signatory_donors` ⇒ 1. |
| PREFER-7 Past/current grantee | **Parent + consortium** | self → parent → prime → cosubs | Any is a grantee of this donor ⇒ tier met. |
| PREFER-7 Donor already engaged | **Parent + consortium** | self → parent → prime → cosubs | Reviewer per-RFP answer still wins first. |
| PREFER-7 Shared collaborator | **Parent + consortium** | self → parent → prime → cosubs | Any member's partner also a donor partner ⇒ met. |
| PREFER-8 Track record | **Parent** | self → parent (**max** rating) | Take the strongest track-record rating across self+parent for the call's program area. |
| PREFER-8 Portal familiarity | **Parent** | self → parent | Parent's `org_donor_registrations` also credited. **+ US-federal fix (§5.3).** |
| PREFER-8 Established (age) | **Parent** | self → parent | Parent's founding year available if child is newer. |
| MUST-2 Strategic fit | **Self only** | self | Our own strategy — never inherited. |
| MUST-3 Capacity | **Self only** | self | The implementing entity's own delivery capacity. |
| MUST-4 Geographic presence | **Consortium** | self → prime → cosubs → parent | A Sub inherits the Prime's (or a co-Sub's / parent's) in-country presence — the consortium delivers in the work geography even if we do not. Own presence still scores strongest (own=1 · via partner/consortium=0.5). |
| MUST-5 Co/pre-financing capacity | **Self only** | self | Our own balance-sheet capacity. |
| PREFER-6 Funding quality | **Self only** | self | Purely call value vs our target band. |
| PREFER-9 Bid effort | **Self only** | self | Our own deadline/BD-team. |

**Golden rule:** consulting another profile can only ever **raise** a score, never
lower it. If nothing in the class resolves, fall back to self and (for the
Sub-registration case) the **0.5 soft** floor — never fabricate a pass.

---

## 5. `criteria_derive` changes

### 5.1 Registration region → sub-aware + soft 0.5 (`:2119-2128`)

```python
reg_req = _as_list(donor.get("donor_registration_region"))
explicit_any = any(... for r in reg_req)
region = [] if explicit_any else list(reg_req)
if not region and not explicit_any and _is_us_federal(rfp):
    region = ["United States"]

covered = explicit_any or _region_covered_graph(region, graph)   # self→parent→prime
if covered:
    score = 1.0
elif graph.role == "sub":
    score = 0.5            # Prime carries the burden; unclear, not a hard fail
else:
    score = 0.0
```

`_region_covered_graph` tries `_region_covered(region, p)` over `[self, parent,
prime]`. With `derive_qualification` (`:2272`), a 0.5 makes MUST-1 read **"Mostly,
one item unclear"** instead of "No, not eligible" — the owner's stated expectation.
If prime/parent *is* US-registered, it reads **"Yes, fully · 2/2"**.

### 5.2 Authorized signatory & PREFER-7 → graph-aware (`:1184`, `:2338`)

Replace the single-profile matchers with "any profile in class" variants:
`_signatory_donor_match_graph`, `_is_past_grantee_graph`, `_donor_engaged_graph`,
`_shared_collaborator_graph` — each iterates the class's profile list and ORs the
result. The canonical donor matching itself is unchanged.

**Signatory scoring stays honest — never forced.** When no signatory data is recorded
for the applicable donor (self OR parent), the ✗ is *correct* and must remain. The fix
is not to auto-pass; it is to make the signatory a first-class, validated input:
- The profile field `org_authorized_signatory_donors` is surfaced as a **"Leadership
  authorized signatory" multiselect** — a validated donor picker (type-ahead against the
  donor_intel catalogue, plus free-typed entries), so a tenant records the donors for
  which it actually holds leadership signatory authority.
- The **parent** carries the donors the parent org holds authority for; the child
  inherits them through the graph (class: Parent + consortium). A credential the family
  genuinely does not hold still scores 0 — the model reports reality, it does not
  manufacture eligibility.

### 5.3 PREFER-8 portal familiarity → US-federal + parent (`:1600`)

Two independent gaps:
- **Parent:** also check `parent.org_donor_registrations`.
- **US-federal:** a `grants.gov`/`sam.gov` registration should credit **any**
  US-federal call (`_is_us_federal(rfp)` already exists and is trusted by SAM/UEI
  and registration-region), not only calls whose `opportunity_link` host happens to
  be grants.gov. Add:

```python
if _is_us_federal(rfp):
    fed = {"grants.gov", "sam.gov"}
    if any(clean_portal_url(r) in fed for p in profiles
           for r in (p.get("org_donor_registrations") or [])):
        return True
```

This is why the flag is ✗ today: the USDoS APS `opportunity_link` is a state.gov/PDF
URL, so the host match found nothing to compare grants.gov/sam.gov against — even
though those *are* the federal submission portals.

### 5.4 PREFER-8 track record → parent max (`:2457`)

`_track_record_band` takes the **max** org rating across `[self, parent]` for the
call's program area before banding against donor priority. (The `your 0 vs donor 5`
in the case is either an unrated Pandemic-Response key on the child, or a
label-vs-key drift — resolve with the §8 data probe before shipping; the parent-max
change helps regardless.)

---

## 6. Code seams

`core/applicant_graph.py::resolve(rfp, org, tenant_id)` is called at each scoring
entry point and passed **into** the derive/factor functions via a new optional
`graph=None` kwarg (default preserves today's single-tenant behavior exactly):

- `views/review_rfp.py:201` (live review)
- `core/opportunity_scoring.py:104` (crawl/cron)
- `core/auto_scorer.py:2885` (auto-score)

Inside `criteria_derive`, when `graph is None`, build a trivial self-only graph so
every existing test and caller is byte-for-byte unaffected.

---

## 7. RLS, privacy, consent

- **Parent → child** read: authorized by the ownership link. The scoring layer uses
  the **`service_client` (RLS-bypass)** to read **only** a whitelist:
  `org_registered_countries`, `org_authorized_signatory_donors`, `org_funder_history`,
  `org_active_donors`, `org_engaged_donors`, `org_donor_registrations`,
  `trusted_partners`/`partners`, `org_domain_expertise`/`org_domain_ratings`,
  `org_founding_year`. Never surfaced in the child tenant's UI — it only feeds a
  score.
- **Co-applicant tenants**: read the same whitelist **only if**
  `share_for_consortium_scoring = true` on that tenant. Off ⇒ unresolved ⇒ the
  Sub-registration soft-0.5 path applies; relationship tiers stay unmet (we do not
  invent a relationship on another org's behalf).
- **Fail-closed** stays the posture (consistent with the get_client boundary work):
  any resolver error ⇒ self-only graph, never a cross-tenant leak.
- The whitelist read is a **single service-client function** in
  `core/applicant_graph.py`, the one place allowed to cross tenants for scoring, so
  the boundary is auditable.

---

## 8. Phasing / build order

- **P0 — Data probe (read-only, no code):** `scripts/_inspect_scoring_inputs.py`
  (gitignored, de-identified — takes `--slug` + `--uid`). **RAN 2026-08-31 on
  `BE-260831-1210`; findings:**
  - **Registration region** — US-federal (link host = grants.gov) vs the child
    registered in two non-US countries → **design fix** (P3), not data.
    `fatal_decline` fires only on `met is False` (`:2641`); a 0.5 has `met=None`, so
    P3's soft-0.5 removes the auto-Decline with **no gate change**.
  - **Authorized signatory** — the child's `org_authorized_signatory_donors` holds
    several non-US funders but not the US federal funder → **parent-held** (P4), or
    interim data add.
  - **PREFER-7** — correct (no relationship with this funder in history/active/
    engaged; no shared collaborator). Improved by the consortium/parent proxy (P4).
  - **Track record** — **pure data gap**: the call's program-area key
    (`IDs - Pandemic Response`) is not among the child's rated `org_domain_ratings`
    keys. Not key-drift. Fix now in the profile UI (donor priority defaults to 5 →
    rating 2-3 Moderate, 4-5 High).
  - **Portal familiarity** — the child lists `sam.gov` but not `grants.gov`; the call
    link host is `grants.gov`, `donor_website` is `state.gov` → no host match.
    **Confirms §5.3** (credit grants.gov/sam.gov for any `_is_us_federal` call — the
    child's `sam.gov` then qualifies). Interim: add `grants.gov` to registrations.
  - Tenant inventory at probe time: no parent-org tenant exists yet (create it in the
    UI, P6/P1) and link the child to it.
- **P1 — Migration 095 (DONE):** `parent_tenant_id` + `share_for_consortium_scoring`
  + self-parent CHECK + cycle-guard trigger. Self-contained, idempotent, name-free
  (per branch/migration discipline). Run in the Supabase SQL editor.
- **P2 — Graph resolver:** `core/applicant_graph.py` + name→tenant + whitelist read
  + self-only fallback. No scoring change yet.
- **P3 — Registration soft-0.5 + graph (DONE):** §5.1. `graph=` threaded through
  `qualification_factors`/`derive_qualification`/`qualification_bid_strength`/
  `derive_criteria`/`fatal_decline`/`factor_breakdown` (None → byte-for-byte
  single-tenant); `resolve()` wired into review_rfp, opportunity_scoring, auto_scorer
  (fail-closed). Sub → 0.5 "unclear" (non-fatal), inherits a registered prime/parent →
  1.0. `tests/test_must1_sub_registration_graph.py`.
- **P4 — Signatory + PREFER-7 graph (DONE):** §5.2. Shared graph helpers
  (`_sig_match_graph`/`_grantee_graph`/`_shared_graph`/`_engaged_graph` over
  `_graph_profiles`) drive BOTH the PREFER-7 label and its components (never disagree)
  and the MUST-5 signatory item. `graph=` threaded through `compliance_factors`/
  `cofinancing_bid_strength`/`derive_cofinancing`/`derive_funder_relationship`/
  `_relationship_factors`. Honest (0/None when no profile holds it); graph=None
  unchanged. `tests/test_prefer7_signatory_graph.py`.
- **P5 — PREFER-8 portal (US-federal + parent) + track-record parent-max:** §5.3–5.4.
- **P6 — UI:** parent-link picker + `share_for_consortium_scoring` toggle in
  Settings/Tenant admin (reusing the mig-082 create→pending→approve flow), and the
  **"Leadership authorized signatory" validated multiselect** (§5.2) on the org profile.
- **P5b — MUST-4 geographic consortium transfer** (§4, `_geo_presence`): a Sub
  inherits Prime/co-Sub/parent in-country presence (own presence still scores
  strongest).
- **Later:** MUST-1 HQ (item C) consortium transfer — lower frequency (§4).

---

## 9. Tests

- `test_applicant_graph_resolution` — name→tenant, parent link, consent gate,
  one-level cap, unresolved fallback.
- `test_must1_sub_registration_soft` — Sub + unresolved Prime ⇒ reg 0.5 ⇒ MUST-1
  "Mostly, one item unclear"; Prime/parent US-registered ⇒ 1 ⇒ "Yes, fully".
- `test_must1_prime_hard` — Prime not US-registered still hard 0.
- `test_signatory_parent_inherits` — parent lists donor ⇒ MUST-5 signatory ✓.
- `test_prefer7_consortium` — co-applicant grantee ⇒ tier met only when consented.
- `test_prefer8_portal_us_federal` — grants.gov/sam.gov reg credits a US-federal
  call whose link host is state.gov.
- `test_prefer8_trackrecord_parent_max` — parent's higher rating wins.
- `test_graph_none_is_self_only` — `graph=None` reproduces current output exactly
  (regression guard over the existing scoring test suite).
- **Boundary:** `test_no_crosstenant_without_consent` — off-consent co-applicant
  contributes nothing; resolver error ⇒ self-only.
