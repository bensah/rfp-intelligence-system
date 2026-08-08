"""`load_dotenv` that works whether or not python-dotenv is installed.

python-dotenv is a CONVENIENCE — it reads key=value pairs out of a .env file into the
process environment. It is listed in requirements.txt, but a virtualenv drifts (a fresh
checkout, a rebuilt venv, an install that predates the pin) and then EVERY entry point
that touches the database dies at import time with

    ModuleNotFoundError: No module named 'dotenv'

…raised from `db/supabase_client.py`, three frames below whatever the user actually ran.
The traceback names dotenv rather than the thing that is really wrong (the venv), and it
takes down the Streamlit app and every script alike.

Nothing here needs the package: reading `KEY=value` lines is a dozen lines of parsing.
So import the real thing when it is present, and fall back to a minimal reader when it
is not. Deployments that inject configuration through the real environment (Streamlit
Cloud secrets, CI, a container) never needed the file in the first place.

The fallback matches python-dotenv's contract on the parts that matter:
  * `KEY=value`, with optional `export ` prefix
  * `#` comments and blank lines skipped
  * surrounding single/double quotes stripped
  * an existing environment variable is NOT overwritten (dotenv's default)
"""
from __future__ import annotations

import os
from pathlib import Path

try:                                            # the real thing, when it is installed
    from dotenv import load_dotenv              # type: ignore[assignment]

except ImportError:                             # …and a self-contained stand-in when not

    def load_dotenv(dotenv_path: str | os.PathLike | None = None,  # type: ignore[misc]
                    *_args, **_kwargs) -> bool:
        """Read a .env file into os.environ. Returns True if a file was read.

        Never raises: a malformed line is skipped rather than taking down the caller,
        because this runs at import time on the app's critical path."""
        path = Path(dotenv_path) if dotenv_path else Path.cwd() / ".env"
        if not path.is_file():
            # Walk up from the CWD, like python-dotenv's find_dotenv().
            for parent in Path.cwd().parents:
                cand = parent / ".env"
                if cand.is_file():
                    path = cand
                    break
            else:
                return False
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, _, val = line.partition("=")
            key = key.strip()
            if not key:
                continue
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            os.environ.setdefault(key, val)     # do NOT clobber the real environment
        return True


__all__ = ["load_dotenv"]
