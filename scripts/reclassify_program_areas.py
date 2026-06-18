"""Re-derive program_area (+ focus_theme) for RFPs from their title + description,
so a generic crawled value like "Health" becomes specific taxonomy areas that
can MATCH an org's program areas (fixes strategic_fit reading "Neither").

For each rfp_submissions row whose program_area is GENERIC — empty, or none of
its values are canonical taxonomy keys (e.g. "Health") — run
core.program_area_classifier.classify_program_areas(title + brief_description):
  * program_area  <- the matched canonical keys (e.g. ["IDs - Tuberculosis", …])
  * focus_theme   <- the high-level categories (e.g. "Infectious Diseases; …")
Rows that already carry taxonomy keys (human- or previously-classified) are LEFT
ALONE — never clobbers curated values. Skips rows the classifier can't place
(keeps their existing value). Dry-run by default; --commit to write.

  python scripts/reclassify_program_areas.py            # preview
  python scripts/reclassify_program_areas.py --commit   # write
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.program_area_classifier import (        # noqa: E402
    PROGRAM_AREA_KEYWORDS, UNSPECIFIED, category_full, classify_program_areas,
)
from db.supabase_client import get_client          # noqa: E402

_KEYS = set(PROGRAM_AREA_KEYWORDS)


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [s.strip() for s in str(v).split(",") if s.strip()]


def _is_generic(program_area) -> bool:
    """True when no current value is a canonical taxonomy key (so it's empty or
    a catch-all like 'Health' that can't match an org's areas)."""
    cur = _as_list(program_area)
    return not any(v in _KEYS for v in cur)


def main(commit: bool) -> int:
    sb = get_client()
    rows = (sb.table("rfp_submissions")
            .select("uid, opportunity_title, brief_description, program_area, focus_theme")
            .execute().data or [])

    updates = []
    for r in rows:
        if not _is_generic(r.get("program_area")):
            continue                                 # already has taxonomy keys
        text = f"{r.get('opportunity_title') or ''} {r.get('brief_description') or ''}"
        areas = [a for a in classify_program_areas(text) if a != UNSPECIFIED]
        if not areas:
            continue                                 # classifier couldn't place it
        cats = sorted({category_full(a) for a in areas})
        updates.append((r.get("uid"), _as_list(r.get("program_area")), areas, cats))

    print(f"{len(rows)} RFPs; {len(updates)} with a generic/empty program_area "
          f"re-classified from the description:")
    for uid, old, areas, cats in updates:
        print(f"  {uid}: {old or '[]'} -> {areas}  | focus_theme: {'; '.join(cats)}")
    if not updates:
        print("Nothing to update.")
        return 0
    if not commit:
        print("\nDRY RUN — re-run with --commit to write.")
        return 0

    n = 0
    for uid, _old, areas, cats in updates:
        try:
            sb.table("rfp_submissions").update({
                "program_area": areas,
                "focus_theme": "; ".join(cats),
            }).eq("uid", uid).execute()
            n += 1
        except Exception as exc:
            print(f"  warn: {uid}: {exc}")
    print(f"\nUpdated {n} RFPs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(commit="--commit" in sys.argv))
