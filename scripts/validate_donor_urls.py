"""HTTP probe for every URL in config/sources.yaml.

Run after editing the sources file or after adding new donors to catch
404s, redirect loops, and TLS errors before the next scan wastes time on
them. Suggests flipping broken sources to `method: manual`.

Usage:
    python scripts/validate_donor_urls.py
    python scripts/validate_donor_urls.py --timeout 8     # per-request seconds
    python scripts/validate_donor_urls.py --only html     # skip rss/rest_json/manual
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "config" / "sources.yaml"

USER_AGENT = (
    "Mozilla/5.0 (compatible; RFPIS-URL-Probe/1.0)"
)


def _load() -> list[dict]:
    with SOURCES.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return cfg.get("sources", []) or []


def _probe(url: str, timeout: int) -> tuple[str, str]:
    """Return (status, detail)."""
    try:
        # Use GET (HEAD is often blocked / lies). Stream so we don't pull
        # the whole body — first 4KB tells us if the response is real.
        r = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8"},
            timeout=timeout,
            allow_redirects=True,
            stream=True,
        )
        # Consume only a tiny chunk to confirm body delivery.
        next(r.iter_content(chunk_size=4096), None)
        r.close()
        if r.status_code == 200:
            return ("OK   ", f"{r.status_code} (final URL: {r.url})")
        if 300 <= r.status_code < 400:
            return ("REDIR", f"{r.status_code} -> {r.headers.get('Location', '?')}")
        return ("FAIL ", f"{r.status_code} {r.reason}")
    except requests.exceptions.TooManyRedirects:
        return ("LOOP ", "redirect loop")
    except requests.exceptions.SSLError as exc:
        return ("TLS  ", str(exc)[:120])
    except requests.exceptions.Timeout:
        return ("TIME ", f"timeout after {timeout}s")
    except requests.exceptions.ConnectionError as exc:
        return ("CONN ", str(exc)[:120])
    except Exception as exc:
        return ("ERR  ", f"{type(exc).__name__}: {exc}")[:120]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=8)
    ap.add_argument(
        "--only",
        default=None,
        help="Restrict to one method: html / rss / rest_json / manual",
    )
    args = ap.parse_args()

    sources = _load()
    if args.only:
        sources = [s for s in sources if (s.get("method") or "").lower() == args.only.lower()]
    if not sources:
        print("No sources to probe.")
        return

    print(f"Probing {len(sources)} source(s)...\n")
    print(f"{'STATUS':6} {'METHOD':10} {'NAME':50} URL")
    print("-" * 110)

    failed: list[tuple[str, str]] = []
    for s in sources:
        name = (s.get("name") or "")[:48]
        url = s.get("url") or ""
        method = (s.get("method") or "").lower()
        if method == "manual":
            # Skip probe — manual sources are intentionally not scraped.
            print(f"{'SKIP ':6} {method:10} {name:50} {url}  (manual; not probed)")
            continue
        if not url:
            print(f"{'EMPTY':6} {method:10} {name:50} -")
            continue
        status, detail = _probe(url, args.timeout)
        print(f"{status:6} {method:10} {name:50} {url}")
        if not status.startswith("OK"):
            print(f"       {' ':10} {' ':50} -> {detail}")
            failed.append((name, status.strip()))
        # Be polite to small donor sites.
        time.sleep(0.2)

    print()
    if failed:
        print(
            f"[!] {len(failed)} source(s) failed. Consider flipping to "
            "method: manual in config/sources.yaml:"
        )
        for name, status in failed:
            print(f"  - {name:50} {status}")
        sys.exit(1)
    print("[OK] All probed sources responded with 2xx.")


if __name__ == "__main__":
    main()
