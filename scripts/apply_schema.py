"""Print the path to db/schema.sql plus paste-ready instructions.

Supabase's Python SDK does not expose raw DDL execution against the REST API,
so the canonical way to apply the schema is via the SQL editor in the Supabase
dashboard. This helper just dumps the SQL to stdout so you can copy it.

    python scripts/apply_schema.py | clip       # Windows
    python scripts/apply_schema.py | pbcopy     # macOS
"""
from __future__ import annotations

import sys
from pathlib import Path

SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"


def main() -> None:
    if not SCHEMA.exists():
        sys.exit(f"Schema file missing: {SCHEMA}")
    sys.stdout.write(SCHEMA.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
