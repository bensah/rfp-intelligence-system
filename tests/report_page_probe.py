"""Runs the report page under AppTest and prints what it rendered, as JSON.

Driven as a SUBPROCESS by tests/test_report_sections.py. It has to be a subprocess: the report
page is a script-scope Streamlit module, and running it in the same interpreter as the other
AppTest module in this suite makes it render nothing — Streamlit keeps global runtime state, and
our own modules stay bound in sys.modules to whichever `streamlit` was live when first imported.
Purging and re-importing both was not enough. A fresh interpreter is the one isolation that
holds, and it costs a few seconds once.

Not a test module itself — it has no test_* functions, so unittest discovery ignores it.
"""
from __future__ import annotations

import json
import os
import sys
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")


class _Query:
    """Every table comes back empty. That is enough: each section renders its heading before it
    touches data, so an empty database still proves the page executes top to bottom."""

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def neq(self, *a, **k): return self
    def gte(self, *a, **k): return self
    def lte(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def execute(self): return mock.Mock(data=[])


class _Client:
    def table(self, *a, **k): return _Query()


def main() -> int:
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("app_pages/report.py", default_timeout=180)
    at.session_state["app_user"] = {"email": "dev@example.com", "role": "super_user"}
    # Everything below the advanced filter sits behind a Generate gate that ends the script
    # with st.stop(), so without this the page renders the filter and nothing else.
    at.session_state["report_generated"] = True

    with mock.patch("db.supabase_client.get_client", return_value=_Client()), \
         mock.patch("db.supabase_client.service_client", return_value=_Client()):
        at.run()

    print("---PROBE---")
    print(json.dumps({
        "exceptions": [str(e.value)[:400] for e in at.exception],
        "subheaders": [s.value for s in at.subheader],
        "markdown": "\n".join(str(m.value) for m in at.markdown),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
