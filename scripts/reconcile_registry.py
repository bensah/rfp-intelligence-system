"""Keep the registry in lockstep with the catalogue.

  1. Sync: upsert active donor_sources rows missing from source_registry.
  2. Flag: recompute `in_catalogue` for every registry row (True if its base
     domain is already in the catalogue) so future pushes exclude them — no
     duplicates across registry ↔ catalogue.

Run AFTER migration 040 (adds source_registry.in_catalogue).
Usage: python scripts/reconcile_registry.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from core import source_registry as sr  # noqa: E402
import scripts.sync_catalogue_to_registry as sync  # noqa: E402


def main() -> int:
    print("1) syncing catalogue → registry …")
    sync.main(commit=True)
    print("\n2) reconciling in_catalogue flag …")
    res = sr.reconcile_in_catalogue()
    if res.get("error") and "in_catalogue" in str(res["error"]):
        print("   ✗ migration 040 not applied yet — run it, then re-run this.")
    else:
        print(f"   marked in-catalogue: {res['marked']} | "
              f"cleared: {res['cleared']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
