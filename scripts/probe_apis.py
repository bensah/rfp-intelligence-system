"""Probe sites for the JSON API that populates their opportunity listings.

Loads each page in Playwright, captures every XHR/fetch JSON response, and prints
the candidate data endpoints (URL + top-level shape + array sizes) so we can
build robust per-site parsers against the real API instead of scraping the DOM.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from playwright.sync_api import sync_playwright  # noqa: E402

TARGETS = {
    "CEPI": "https://cepi.net/calls-for-proposals",
    "OSF": "https://www.opensocietyfoundations.org/grants",
    "AfDB": "https://www.afdb.org/en/projects-and-operations/procurement",
    "GCGH": "https://gcgh.grandchallenges.org/grant-opportunities",
    "GatesGC": "https://www.grandchallenges.org/grant-opportunities",
    "MITSolve": "https://solve.mit.edu/challenges",
    "GIZ": "https://ausschreibungen.giz.de",
    "RVO": "https://english.rvo.nl/subsidies-programmes",
    "CZI": "https://chanzuckerberg.com/grants-ventures/grants/",
    "Hewlett": "https://hewlett.org/grants/",
    "Packard": "https://www.packard.org/grantees/funding-opportunties/",
    "GoogleOrg": "https://www.google.org/impact-challenges/",
    "NIHR": "https://www.nihr.ac.uk/funding-opportunities",
    "GlobalFundTenders": "https://www.theglobalfund.org/en/business-opportunities/open-tenders/",
}

SKIP = ("google-analytics", "googletagmanager", "doubleclick", "facebook",
        "hotjar", "segment.", "cookielaw", "onetrust", "/gtm", "sentry",
        "cloudflareinsights", "/collect", "linkedin", "/recaptcha", "fonts.")


def shape(obj, depth=0):
    if isinstance(obj, dict):
        keys = list(obj.keys())[:12]
        return "{" + ", ".join(keys) + ("…" if len(obj) > 12 else "") + "}"
    if isinstance(obj, list):
        return f"[{len(obj)} items] " + (shape(obj[0], depth + 1) if obj else "")
    return type(obj).__name__


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for label, url in TARGETS.items():
            page = browser.new_page(user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"))
            hits = []

            def on_response(resp):
                u = resp.url
                if any(s in u.lower() for s in SKIP):
                    return
                ct = (resp.headers or {}).get("content-type", "")
                if "json" not in ct.lower():
                    return
                try:
                    body = resp.json()
                except Exception:
                    return
                txt = json.dumps(body)[:200000]
                # heuristic: looks like it carries opportunity-ish data
                low = txt.lower()
                if len(txt) < 200:
                    return
                score = sum(low.count(k) for k in (
                    "title", "deadline", "grant", "call", "opportunit",
                    "proposal", "challenge", "tender", "award", "funding"))
                hits.append((score, len(txt), u, shape(body)))

            page.on("response", on_response)
            try:
                page.goto(url, wait_until="networkidle", timeout=45000)
            except Exception as exc:
                print(f"\n### {label}  {url}\n    goto: {str(exc)[:70]}")
            try:
                page.wait_for_timeout(2500)
            except Exception:
                pass
            print(f"\n{'='*80}\n### {label}  {url}")
            hits.sort(key=lambda x: -x[0])
            if not hits:
                print("    no JSON endpoints captured")
            for score, ln, u, sh in hits[:6]:
                print(f"    score={score:>4} len={ln:>7}  {u[:88]}")
                print(f"               shape={sh[:120]}")
            page.close()
        browser.close()


if __name__ == "__main__":
    main()
