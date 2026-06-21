"""Apply Bernard's assistant-researched source register to source_registry.

Faithfully maps the researched columns (Source class / Verification / Access /
Recommended ingestion / Notes) onto our schema:
  Source class  -> source_class (simplified: Primary source / Opportunity
                   Aggregator / Application-resource host) + derived classification
                   (primary | aggregator) the scanner gate uses.
  Verification  -> status (confirmed when "…verified" and not "needs/not canonical").
  Access        -> access_model ; Recommended ingestion -> ingestion_method.
  Notes         -> notes (existing column).

Upsert by host (idempotent). DRY-RUN by default — prints the mapping so it can be
eyeballed before writing.

  python scripts/update_source_registry.py            # preview mapping
  python scripts/update_source_registry.py --commit   # upsert to source_registry
"""
from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from db.supabase_client import get_client   # noqa: E402

# (host, source_class_text, verification_text, access_text, ingestion_text, notes)
DATA = [
 ("grants.gov", "Official multi-agency grant portal / primary source", "Primary verified", "Free", "API", "Best-in-class U.S. federal funding source with documented public search/fetch APIs."),
 ("developmentaid.org", "Development-sector intelligence platform / aggregator", "Aggregator verified", "Freemium / paid", "Licensed integration if available; otherwise page crawl if permitted", "Jobs, procurement, grants, consultants, donor intelligence. Discovery, not canonical."),
 ("globalsouthopportunities.com", "Opportunity aggregator", "Aggregator verified", "Free public web", "Page crawl / RSS if available", "Curates jobs, scholarships, grants, fellowships, esp. Global South."),
 ("gov.uk", "Official UK government portal / primary source", "Primary verified", "Free", "Page crawl", "Use GOV.UK international development funding finder + Find a Grant."),
 ("international.gc.ca", "Official Global Affairs Canada funding portal / primary source", "Primary verified", "Free read; application portal/login may apply", "Page crawl", "Official Global Affairs Canada international assistance funding opportunities."),
 ("idrc-crdi.ca", "Official IDRC funding portal / primary source", "Primary verified", "Free read", "Page crawl", "IDRC publishes open calls + funding opportunities directly."),
 ("researchnet-recherchenet.ca", "Official Canadian research application portal", "Official application portal verified", "Free search/read; login to apply", "Page crawl / dynamic crawl with caution", "ResearchNet = CIHR application portal; applicants need accounts + CIHR PINs."),
 ("gcgh.grandchallenges.org", "Grand Challenges / Gates-linked primary programme source", "Primary verified", "Free read; application varies", "Page crawl", "Primary Grand Challenges source; also canonicalise against grandchallenges.org."),
 ("wellcome.org", "Official funder portal / primary source", "Primary verified", "Free read; login to apply", "Page crawl", "Wellcome publishes searchable funding schemes; funding platform for applications."),
 ("grandchallenges.ca", "Official Grand Challenges Canada funding portal / primary source", "Primary verified", "Free read; applications only when calls open", "Page crawl", "Official Apply-for-funding page; no unsolicited proposals outside open calls."),
 ("theglobalfund.org", "Official funder/procurement source / primary source", "Primary verified", "Free read", "Page crawl; document parsing", "Global Fund business, consultancy, tender, RFP/REOI under Business & Consultancy Opportunities."),
 ("gavi.org", "Official funder/procurement/jobs source / primary source", "Primary verified", "Free read", "Page crawl + document parsing", "Gavi publishes RFPs, EOIs, consulting opportunities, vacancies directly."),
 ("fondationpierrefabre.org", "Official foundation funding portal / primary source", "Primary verified", "Free read", "Page crawl", "Calls for projects incl. eHealth and albinism-related opportunities."),
 ("nihr.ac.uk", "Official UK health research funder portal / primary source", "Primary verified", "Free read; application systems may require login", "Page crawl", "NIHR publishes current funding opportunities, programmes, award information."),
 ("international-partnerships.ec.europa.eu", "Official EC international partnerships funding source", "Primary verified", "Free read; EU systems for application", "Page crawl; canonical calls via EU Funding & Tenders", "INTPA funding guidance; many live calls resolve to the EU Funding & Tenders Portal."),
 ("norad.no", "Official Norad grant/call source / primary source", "Primary verified", "Free read; grants portal/login for applications", "Page crawl", "Norad publishes calls; applications via Norad/MFA grant systems."),
 ("globalinnovation.fund", "Official funder portal / primary source", "Primary verified", "Free read; online application form", "Page crawl", "Global Innovation Fund accepts applications through its own process."),
 ("ec.europa.eu", "Official EU funding/tenders portal host / primary source", "Primary verified", "Free read; EU Login for applications", "API + page crawl", "EU Funding & Tenders: official APIs; canonical for many EU grants and tenders."),
 ("cm.usembassy.gov", "Official U.S. Embassy country-mission grant source / primary source", "Primary verified", "Free read; application may use email/forms/SAM.gov/Grants.gov", "Page crawl; canonicalise to Grants.gov where applicable", "U.S. Embassy Cameroon grant opportunities + public diplomacy notices."),
 ("civil-protection-humanitarian-aid.ec.europa.eu", "Official DG ECHO / EU humanitarian source", "Primary verified", "Free read; application via EU systems or partner mechanisms", "Page crawl; EU Funding & Tenders where applicable", "EU humanitarian + civil protection calls, financing decisions."),
 ("comicrelief.com", "Official foundation funding portal / primary source", "Primary verified", "Free read", "Page crawl", "Comic Relief publishes live + closed funding opportunities directly."),
 ("unitaid.org", "Official funder portal / primary source", "Primary verified", "Free read", "Page crawl + document parsing", "Unitaid publishes calls for proposals + funding application information directly."),
 ("docs.google.com", "External form/document host", "Not canonical; needs primary-source confirmation", "Public/private by link", "Do not seed-crawl; ingest only as linked artifact", "Google Forms/Docs = application/supporting material only. Keep referring primary URL."),
 ("global-health-edctp3.europa.eu", "Official EU partnership programme source / primary source", "Primary verified", "Free read; EU systems for applications", "EU Funding & Tenders API + page crawl", "EDCTP3 calls are published via the EC Funding & Tenders Portal."),
 ("jica.go.jp", "Official donor/procurement source / primary source", "Primary verified", "Free read", "Page crawl + document parsing", "JICA ODA grant, procurement, bidding info across central + country pages."),
 ("healthresearch.org", "Official funding/procurement source / primary source", "Primary verified", "Free read", "Page crawl + document parsing", "Health Research, Inc. publishes RFA/RFP documents + funding opportunities directly."),
 ("resources.theglobalfund.org", "Official Global Fund resource repository", "Official resource host verified", "Free public documents", "Document/page crawl; not primary discovery", "Application materials + grant lifecycle documents, not main discovery host."),
 ("calls.sida.se", "Official Sida call/application workspace", "Primary verified", "Free read; application workflow may require account", "Page crawl / dynamic crawl", "Sida calls + application-process pages."),
 ("ungm.org", "Official UN procurement platform / multi-agency primary source", "Primary verified", "Free search/read; registration + Pro features for some functions", "API if approved; otherwise page/search crawl", "Official UN procurement platform: EOI, RFP, RFQ, ITB, consultants, partners."),
 ("povertyactionlab.org", "Official J-PAL funding/call source / primary source", "Primary verified", "Free read", "Page crawl", "J-PAL publishes research funding calls + RFPs directly."),
 ("www2.fundsforngos.org", "Funding-opportunity aggregator", "Aggregator verified", "Freemium / public pages + premium layers", "Page crawl if permitted; primary-source confirmation required", "Discovery; canonicalise every opportunity back to the funder."),
 ("cdc.gov", "Official U.S. agency guidance/source", "Primary verified, but canonical NOFO source is Grants.gov", "Free", "Grants.gov API for opportunities + CDC page crawl for context", "CDC: all CDC grant/cooperative-agreement opportunities are posted on Grants.gov."),
 ("ocrahope.org", "Official foundation/research grant source / primary source", "Primary verified", "Free read", "Page crawl", "OCRA publishes grant programmes + application information directly."),
 ("ahpsr.who.int", "Official WHO-hosted AHPSR call source / primary source", "Primary verified", "Free read", "Page crawl + document parsing", "AHPSR calls for proposals + funding opportunities, esp. LMIC institutions."),
 ("solve.mit.edu", "Official challenge/prize platform / primary source", "Primary verified", "Free read; account/submission portal for applications", "Page crawl / dynamic crawl", "MIT Solve runs challenge-based opportunities + funding for selected innovators."),
 ("portal365.org", "Grant-management platform / opportunity aggregator", "Aggregator verified", "Freemium / paid features", "Page crawl; primary-source confirmation required", "Lists funding opportunities; states it is not a donor and does not fund."),
 ("thepandemicfund.org", "Official fund portal / primary source", "Primary verified", "Free read; official application process", "Page crawl + document parsing", "Pandemic Fund publishes official calls for proposals + awarded grants."),
 ("scidev.net", "Science/development media + noticeboard aggregator", "Aggregator verified", "Free", "RSS/feed for stories; page crawl noticeboard", "Discovery of jobs/grants/events/announcements; not canonical for verification."),
 ("fundsforngospremium.com", "Paid grant database / aggregator", "Aggregator verified", "Paid / login", "Licensed integration only; avoid unauthorised crawling", "Premium grants database; opportunities need primary-source confirmation."),
 ("unfpa.org", "Official UNFPA source; procurement canonicalised through UNGM", "Primary verified", "Free read; UNGM registration for suppliers", "UNGM ingestion + UNFPA page crawl", "UNFPA publishes calls; supplier procurement routed through UNGM."),
 ("zayedsustainabilityprize.com", "Official prize/award source / primary source", "Primary verified", "Free read; submission portal", "Page crawl", "Official source for Zayed Sustainability Prize deadlines, categories, prize info."),
 ("gacd.org", "Official alliance funding-call source / primary source", "Primary verified", "Free read", "Page crawl; also track member funder pages", "GACD publishes current + future calls; member agencies issue joint calls."),
 ("catalyticopportunityfund.org", "Official catalytic fund/opportunity source / primary source", "Primary verified", "Free read", "Page crawl", "Publishes product-specific funding streams + opportunity details."),
 ("fundinnovation.dev", "Official Fund for Innovation in Development portal / primary source", "Primary verified", "Free read; online portal for applications", "Page crawl; do not crawl application portal unless authorised", "FID call rules, funding stages, application guidance."),
 ("thecatalystfund.com", "Official venture fund / accelerator source", "Primary verified", "Free read; pitch/application form", "Page crawl", "Not a grant source strictly; venture/accelerator funding for startups."),
 ("globalfundcommunityfoundations.org", "Official foundation/grantmaker source / primary source", "Primary verified", "Free read", "Page crawl", "GFCF small grants for community philanthropy organisations."),
 ("frld.org", "Official climate fund source / primary source", "Primary verified", "Free read", "Page crawl + document parsing", "Fund for Responding to Loss and Damage; official site confirmed by UNFCCC."),
 ("cdn.pfizer.com", "CDN/document host, not canonical source", "Needs primary-source confirmation", "Public if file URL is known", "Linked-document parsing only", "Use pfizer.com grants pages as canonical; parse PDFs hosted on the CDN."),
 ("cgdev.org", "Official think tank / occasional RFP source", "Primary verified for CGD-published calls; not a general grant portal", "Free read", "Page crawl + document parsing", "Use only CGD pages/documents that explicitly publish the opportunity."),
 ("grants.nih.gov", "Official NIH grants information portal / primary source", "Primary verified, but Grants.gov is canonical for NIH NOFOs", "Free", "Grants.gov API for NOFOs + NIH RSS/page crawl for guidance", "NIH NOFOs posted on Grants.gov; NIH tools + RSS/email feeds for updates."),
 ("nestlefoundation.org", "Official foundation research-grant source", "Primary verified", "Free read; proposal submission process applies", "Page crawl + document/template parsing", "Nutrition research grant categories, eligibility, application guidance."),
 ("cepi.net", "Official funder / R&D call source", "Primary verified", "Free read; application process per call", "Page crawl + document parsing", "CEPI official calls for vaccine + epidemic/pandemic preparedness programmes."),
 ("sidaction.org", "Official funder / call-for-proposals source", "Primary verified", "Free read; application may use external platform", "Page crawl + document parsing", "Thematic HIV research calls; recent instructions refer to Synto for applications."),
 ("averydennison.com", "Official corporate foundation grant source", "Primary verified", "Free read; online eligibility/application process", "Page crawl", "Avery Dennison Foundation accepts grant requests year-round; guidance on site."),
 ("grants.chinnova.aau.org", "Official grant-management/application portal", "Official application host verified", "Free read; submission portal", "Dynamic page crawl; also crawl chinnova.aau.org for call context", "CHINNOVA call pages direct applicants to submit through this portal."),
 ("leap-re.eu", "Official AU-EU research call source", "Primary verified", "Free read; application rules vary by funder", "Page crawl + document parsing", "LEAP-RE/LEAP-SE AU-EU collaborative R&I calls; eligibility per participating funders."),
 ("div.fund", "Official Development Innovation Ventures funding source", "Primary verified", "Free read; application portal/process", "Page crawl; application portal only if authorised", "DIV funding guidance + RFP/application info for development innovations."),
 ("fundingprogrammesportal.gov.cy", "Official Cyprus government funding portal", "Primary verified", "Free public portal", "Page crawl / structured search crawl", "Cyprus funding programmes + calls across EU, national, other schemes."),
 ("submit.gatesfoundation.org", "Official Gates Foundation RFP application portal", "Official application host verified", "Free read; sign-up/login may apply", "Page crawl for RFP list; canonicalise to gatesfoundation.org", "SurveyMonkey Apply-hosted Gates RFP portal. Application host, not main source."),
 ("google.org", "Official corporate philanthropy / challenge source", "Primary verified", "Free read; applications only during open calls", "Page crawl", "Google.org publishes Impact Challenges + programme-specific funding calls."),
 ("worldbank.org", "Official multilateral development bank source", "Primary verified", "Free read; procurement systems may require registration", "World Bank procurement data/API where available + page crawl", "Procurement notices, project/corporate procurement, jobs, projects, programmes."),
 ("sida.se", "Official Swedish development agency funding/calls source", "Primary verified", "Free read; application process varies", "Page crawl; use calls.sida.se for call workspaces", "Sida calls, announcements, research grants, financial-support guidance."),
 ("worlddiabetesfoundation.submittable.com", "Official WDF application portal", "Official application host verified", "Free read; Submittable login/application workflow", "Do not seed-crawl; capture as linked application portal", "Use worlddiabetesfoundation.org as primary; this Submittable host = application manager."),
 ("globalhealth.stanford.edu", "Official university global-health grant source", "Primary verified", "Free read; eligibility often Stanford-linked", "Page crawl", "Stanford Global Health seed grants + calls for proposals."),
 ("internationalcancerfoundation.org", "Official foundation grant/fellowship source", "Primary verified", "Free read; application process per programme", "Page crawl", "ICF fellowship, scholarship, travel-grant, training for cancer-care professionals."),
 ("openphilanthropy.org", "Official philanthropic funder source (now Coefficient Giving)", "Primary verified", "Free read; application forms vary", "Page crawl + linked form capture", "Open Philanthropy now Coefficient Giving; publishes funds, RFPs, application routes."),
 ("rwjf.org", "Official foundation funding source", "Primary verified", "Free read; application windows vary", "Page crawl + funding-alert monitoring", "RWJF funding opportunities, open calls, challenges, grant-process guidance."),
 ("government.nl", "Official Netherlands government funding/grant source", "Primary verified", "Free read", "Page crawl + document parsing", "Dutch development-cooperation grant programmes + subsidy frameworks."),
 ("biswasfamilyfoundation.org", "Official foundation grant source", "Primary verified", "Free read; application cycles apply", "Page crawl", "Fast Grants + other science/global-health funding directly."),
 ("giz.de", "Official implementer/procurement/call source", "Primary verified", "Free read; e-procurement platform may require registration", "Page crawl + GIZ e-procurement/TED/Bund.de monitoring", "GIZ tenders, calls for proposals, EOIs; formal tenders also on e-procurement/Bund.de/TED."),
 ("robertcarrfund.org", "Official funder / RFP source", "Primary verified", "Free read; application portal during calls", "Page crawl + document parsing", "Robert Carr Fund publishes RFPs + calls for expressions of interest directly."),
 ("ciff.org", "Official foundation/funder source", "Primary verified", "Free read; open-call model not clearly established", "Page crawl for funding announcements + strategy pages", "Primary foundation source; not a general open-call portal — mark unsolicited status separately."),
 ("opportunitysquare.org", "Opportunity/grant aggregator and support platform", "Aggregator verified", "Free public listings; services may be paid", "Page crawl; primary-source confirmation required", "Grants Zone curates grants for African entrepreneurs, businesses, NGOs."),
 ("ikeafoundation.org", "Official foundation grant/source-of-awards site", "Primary verified for grant portfolio; not open application source", "Free read", "Page crawl for awarded grants/partner intelligence", "Publishes active grants but does not accept unsolicited proposals."),
 ("grantbite.com", "Grant-discovery platform / aggregator", "Aggregator verified", "Freemium / platform-based", "Licensed integration if available; otherwise page crawl if permitted", "Grant discovery, eligibility, AI drafting, pipeline, alerts. Discovery, not canonical."),
 ("gatesfoundation.org", "Official foundation funding/RFP source", "Primary verified", "Free read; most grants invited; RFPs occasionally published", "Page crawl + application-portal capture", "Most grants invited; occasionally awards via published RFPs."),
]

_AGG = ("aggregator", "intelligence platform", "discovery platform",
        "grant-discovery", "noticeboard", "grant database", "grants database",
        "media +")
_HOST = ("application portal", "application host", "resource host",
         "resource repository", "document host", "cdn", "external form",
         "form/document")


def norm_class(t: str) -> tuple[str, str]:
    tl = t.lower()
    if any(k in tl for k in _AGG):
        return "Opportunity Aggregator", "aggregator"
    if any(k in tl for k in _HOST):
        return "Application/resource host", "aggregator"
    return "Primary source", "primary"


def norm_status(v: str) -> str:
    vl = v.lower()
    if "needs" in vl or "not canonical" in vl:
        return "pending"
    return "confirmed" if "verified" in vl else "pending"


def norm_access(t: str) -> str:
    tl = t.lower()
    if "freemium" in tl:
        return "Freemium"
    if "paid" in tl and "free" not in tl:
        return "Paid"
    if "public/private" in tl:
        return "Unknown"
    if "login required" in tl and "free" not in tl:
        return "Login required"
    if "free" in tl or "public" in tl:
        return "Free"
    return "Unknown"


def norm_ingest(t: str) -> str:
    tl = t.lower()
    if "api" in tl:
        return "API"
    if "rss" in tl:
        return "RSS"
    if any(k in tl for k in ("do not seed", "linked-document", "linked artifact",
                             "licensed integration", "manual")):
        return "manual review"
    return "page crawl"


def main(commit: bool) -> int:
    rows = []
    for host, sc, vf, ac, ing, notes in DATA:
        source_class, classification = norm_class(sc)
        rows.append({
            "host": host, "source_class": source_class,
            "classification": classification, "status": norm_status(vf),
            "access_model": norm_access(ac), "ingestion_method": norm_ingest(ing),
            "notes": (notes or "")[:600] or None,
            "verified_by": "assistant-research",
            "verified_at": datetime.now(timezone.utc).isoformat(),
        })
    print(f"hosts: {len(rows)}")
    print("source_class:", dict(Counter(r["source_class"] for r in rows)))
    print("classification:", dict(Counter(r["classification"] for r in rows)))
    print("status:", dict(Counter(r["status"] for r in rows)))
    print("\nper-host mapping (eyeball before --commit):")
    for r in rows:
        print(f"  {r['host']:<42} {r['source_class']:<24} "
              f"{r['classification']:<10} {r['status']:<9} "
              f"{r['access_model']:<13} {r['ingestion_method']}")
    if not commit:
        print("\nDRY RUN — re-run with --commit to upsert.")
        return 0
    sb = get_client()
    n = 0
    for i in range(0, len(rows), 100):
        sb.table("source_registry").upsert(rows[i:i + 100],
                                           on_conflict="host").execute()
        n += len(rows[i:i + 100])
    print(f"\nUpserted {n} hosts into source_registry.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--commit" in sys.argv))
