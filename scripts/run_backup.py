"""Headless weekly database backup — incremental, off-site, skip-when-empty.

Run by the "Database backup" GitHub Actions workflow (.github/workflows/backup.yml,
Sundays). The Supabase Free plan has no managed backups, so this is the failsafe:

  1. Read the last-backup watermark (core.backup.get_watermark).
  2. Build an INCREMENTAL ZIP — only rows changed since the watermark (full on first run).
     If nothing is new → skip (no upload, watermark unchanged).
  3. Upload to OneDrive (core.onedrive) and/or keep a local copy for a CI artifact.
  4. Advance the watermark ONLY after the backup is durably stored, so a failed upload
     retries the same range next week.

Env: SUPABASE_URL/KEY (required), ONEDRIVE_* (enable upload), BACKUP_OUT (artifact path).
Exits non-zero if OneDrive is configured but the upload fails.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    from core import backup, onedrive

    since = backup.get_watermark()
    print(f"Last-backup watermark: {since or '(none — first run → full backup)'}")
    try:
        data, manifest, until = backup.build_incremental_backup(since)
    except Exception as exc:
        print(f"ERROR: backup export failed: {type(exc).__name__}: {exc}")
        return 1

    if data is None:
        print(f"No new data since {since} — nothing to back up. Skipping "
              f"(counts: {manifest.get('tables')}).")
        return 0

    mode = manifest.get("mode", "full")
    fn = backup.backup_filename(mode, since, until)
    print(f"Built {fn} — {mode}, new_rows={manifest.get('new_rows')}, "
          f"{len(data):,} bytes.")

    wrote_artifact = False
    out = os.getenv("BACKUP_OUT")
    if out:
        try:
            p = Path(out)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            wrote_artifact = True
            print(f"Wrote local copy: {p}")
        except Exception as exc:
            print(f"WARN: couldn't write BACKUP_OUT ({exc})")

    od_configured = onedrive.is_configured()
    od_ok = False
    if od_configured:
        try:
            res = onedrive.upload_bytes(data, fn)
            od_ok = True
            print(f"Uploaded to OneDrive [{onedrive.folder_label()}]: "
                  f"{res.get('name') or fn}"
                  + (f" ({res['webUrl']})" if res.get('webUrl') else ""))
        except Exception as exc:
            print(f"ERROR: OneDrive upload failed: {type(exc).__name__}: {exc}")
    else:
        print("ONEDRIVE_* not configured — keeping the artifact copy only.")

    # Advance the watermark only when the backup is durably stored: a successful OneDrive
    # upload (preferred), or — while OneDrive isn't set up yet — the artifact write.
    advance = od_ok or (not od_configured and wrote_artifact)
    if advance:
        try:
            backup.set_watermark(until)
            print(f"Watermark advanced to {until}.")
        except Exception as exc:
            print(f"WARN: couldn't persist watermark ({exc}) — next run may re-include "
                  "these rows (harmless).")
    else:
        print("Watermark NOT advanced (backup not durably stored) — will retry the same "
              "range next run.")

    if od_configured and not od_ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.exit(main())
