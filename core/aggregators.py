"""Aggregator / non-primary source detection.

Distinguishes PRIMARY opportunity sources from third-party AGGREGATORS:
  * primary    — a platform where DONORS themselves post their RFPs (a funder's own
                 site, an employer's careers page). A primary site naturally has
                 LISTING/index pages of its own calls — that's expected, NOT an
                 aggregator. We crawl those listings to reach the individual call
                 pages. (Listing-PAGE rejection — "this URL indexes calls, store
                 the children not the index" — lives in auto_scorer at the URL
                 level; it is NOT a host classification.)
  * aggregator — a THIRD PARTY that pulls RFPs from primary sources and republishes
                 on its own platform (GrantBite, DevelopmentAid, FundsforNGOs, …).
                 A discovery SEED only; the pipeline resolves it to the donor's own
                 page (core.source_resolver) before the gate. Never stored as-is
                 (copyright + duplicate-content/SEO).
  * blog       — blog-platform hosts (blogspot, wordpress.com, medium, substack…).
                 What let `grants-gov.blogspot.com` slip in.

Layering: a HUMAN-CONFIRMED entry in core.source_registry is authoritative; then a
known-primary allowlist; then known aggregators / blog platforms; else 'unknown'
(no opinion — NOT rejected, just logged for a human to classify once). Everything
is best-effort and fails OPEN — a detector error never blocks a scan.
"""
from __future__ import annotations

from core.source_registry import confirmed_class, normalize_host

# Known PRIMARY portals that aggregate by MANDATE (the donor's own publishing
# platform), so they must be treated as primary, never as aggregators:
#   grants.gov / sam.gov — the US federal government publishes ALL its grants /
#       contracts here instead of on each agency's own site, so this IS the donor
#       source for US federal opportunities.
#   reliefweb.int — explicit exception (OCHA's humanitarian opportunities portal).
_KNOWN_PRIMARY = ("grants.gov", "sam.gov", "reliefweb.int")

# Known competitor aggregators — third-party republishers, matched as a host
# substring. Curated seed list; unknown ones get logged to source_registry for
# human confirmation, after which confirmed_class() overrides this. NB: ReliefWeb
# is deliberately NOT here — it's a primary exception (see _KNOWN_PRIMARY).
_KNOWN_AGGREGATORS = (
    # Grant databases / intelligence platforms
    "grantbite.com", "instrumentl.com", "developmentaid.org", "devex.com",
    "fundsforngos.org", "fundsforngo.com", "fundsforngos.com",
    "grantstation.com", "grantforward.com", "opengrants.io", "grantwatch.com",
    "grantgateway", "grantgopher.com", "pivot.proquest.com", "candid.org",
    "terravivagrants.org", "grantnav", "ngobox.org", "fundsforcompanies.com",
    # Job aggregators (UN / development / general)
    "impactpool.org", "unjobs.org", "unjobnet.org", "untalent.org",
    "adzuna.com", "jooble.org", "theirstack.com",
    # Opportunity aggregators (scholarships / fellowships / youth / Africa)
    "opportunitydesk.org", "opportunitiesforafricans.com",
    "globalopportunitydesk.com", "youthop.com", "opportunitiesforyouth.org",
    "afterschoolafrica.com", "opportunitiesforafricanwomen.org",
    "opportunitiesfeed.com", "opportunitiescorners.com", "greatyop.com",
    # Tender / procurement aggregators
    "globaltenders.com", "tendersontime.com", "dgmarket.com",
    "tenderimpulse.com", "biddetail.com", "openopps.com",
)

# Blog / self-publish platforms — never the primary host of a funder's call.
_BLOG_PLATFORMS = (
    "blogspot.com", "wordpress.com", "medium.com", "substack.com",
    "tumblr.com", "blogger.com", "wixsite.com", "weebly.com", "ghost.io",
    "livejournal.com", "typepad.com", "over-blog.com",
)


def _host_hits(host: str, needles) -> bool:
    return any(n in host for n in needles)


def classify(url: str | None, title: str | None = None) -> tuple[str, str]:
    """Return (kind, reason). kind ∈ primary|aggregator|blog|unknown.

    Precedence: human-confirmed registry → known-primary allowlist → known
    aggregator → blog platform → 'unknown'. 'unknown' means 'no opinion' — the
    caller must NOT reject on it, only log it. There is deliberately NO host-level
    'listing' kind: a listing page on a primary donor site is still primary."""
    host = normalize_host(url)
    if not host:
        return "unknown", "no host"
    try:
        c = confirmed_class(host)
        if c:
            return c, f"registry-confirmed: {host}"
    except Exception:
        pass
    if _host_hits(host, _KNOWN_PRIMARY):
        return "primary", f"known primary portal: {host}"
    if _host_hits(host, _KNOWN_AGGREGATORS):
        return "aggregator", f"known aggregator: {host}"
    if _host_hits(host, _BLOG_PLATFORMS):
        return "blog", f"blog platform: {host}"
    return "unknown", host


# Per-host taxonomy for the source catalogue (Bernard's standard): host substring
# → (source_class, access_model, ingestion_method, has_api). Hosts not listed get
# defaults derived from classify() below. Source-class values are from the agreed
# vocabulary; several aggregators have official APIs/feeds worth ingesting at scale.
_HOST_META = (
    # Primary portals with official APIs / feeds
    ("grants.gov", "Primary source", "Free", "API", True),
    ("sam.gov", "Primary source", "Free", "API", True),
    ("reliefweb.int", "Primary source", "Free", "API", True),
    # Grant databases / intelligence platforms
    ("instrumentl.com", "Grant database", "Paid", "page crawl", False),
    ("developmentaid.org", "Intelligence platform", "Paid", "page crawl", False),
    ("devex.com", "Intelligence platform", "Paid", "page crawl", False),
    ("grantstation.com", "Grant database", "Paid", "page crawl", False),
    ("grantforward.com", "Grant database", "Freemium", "page crawl", False),
    ("grantwatch.com", "Grant database", "Paid", "page crawl", False),
    ("fundsforngo", "Grant database", "Freemium", "RSS", True),  # has /feed
    ("terravivagrants.org", "Grant database", "Free", "page crawl", False),
    ("candid.org", "Grant database", "Paid", "page crawl", False),
    ("opengrants.io", "Grant database", "Freemium", "page crawl", False),
    ("grantgopher.com", "Grant database", "Paid", "page crawl", False),
    # Job aggregators (several with APIs/feeds)
    ("adzuna.com", "Job aggregator", "Freemium", "API", True),
    ("jooble.org", "Job aggregator", "Freemium", "API", True),
    ("theirstack.com", "API provider", "Paid", "API", True),
    ("untalent.org", "Job aggregator", "Free", "RSS", True),
    ("impactpool.org", "Job aggregator", "Freemium", "page crawl", False),
    ("unjobs.org", "Job aggregator", "Free", "page crawl", False),
    ("unjobnet.org", "Job aggregator", "Free", "page crawl", False),
    # Tender / procurement aggregators
    ("globaltenders.com", "Tender database", "Paid", "page crawl", False),
    ("tendersontime.com", "Tender database", "Paid", "page crawl", False),
    ("dgmarket.com", "Tender database", "Freemium", "page crawl", False),
    ("tenderimpulse.com", "Tender database", "Freemium", "page crawl", False),
    ("biddetail.com", "Tender database", "Paid", "page crawl", False),
    ("openopps.com", "Tender database", "Freemium", "page crawl", False),
)
_CLASS_DEFAULT = {"primary": "Primary source",
                  "aggregator": "Opportunity Aggregator",
                  "blog": "Opportunity Aggregator", "unknown": ""}


def meta(url: str | None) -> dict:
    """Source-catalogue taxonomy for a host: {source_class, access_model,
    ingestion_method, has_api}. Curated where known; else derived from the
    aggregator/primary/blog classification (access_model 'Unknown')."""
    host = normalize_host(url)
    for sub, sc, am, im, api in _HOST_META:
        if sub in host:
            return {"source_class": sc, "access_model": am,
                    "ingestion_method": im, "has_api": api}
    kind = classify(url)[0]
    return {"source_class": _CLASS_DEFAULT.get(kind, ""),
            "access_model": "Unknown", "ingestion_method": "page crawl",
            "has_api": False}


def is_non_primary(url: str | None, title: str | None = None) -> tuple[bool, str]:
    """True (+reason) when a source must NOT be published as-is — a confirmed/known
    AGGREGATOR or BLOG. Primary and unknown → False (allowed; unknown is logged for
    review). Note: a listing PAGE is not non-primary here — auto_scorer rejects
    listing URLs separately so we crawl them for their child calls."""
    kind, why = classify(url, title)
    if kind in ("aggregator", "blog"):
        return True, f"{kind}: {why}"
    return False, why


def is_aggregator(url: str | None) -> bool:
    """Aggregator-kind only (drives the title→primary-source resolve step).
    Broader than source_resolver's hardcoded regex — also fires for any host a
    human has confirmed as 'aggregator' in the registry."""
    return classify(url)[0] == "aggregator"
