"""Resilience stress test — prove the app survives internet disruptions / high traffic.

Runs OFFLINE by default (fault injection needs no live DB); a couple of checks light up
only when Supabase creds are configured. Exercises:

  1. safe_execute retry/backoff  — transient httpx errors retry then succeed;
     exhausted retries re-raise; a NON-transient error raises immediately (no wasted retries).
  2. is_connectivity_error       — classifies ConnectError / wrapped ReadTimeout as transient,
     real bugs (ValueError) as not.
  3. Scan DB touchpoints degrade — with the DB permanently failing, seen_ledger /
     extracted_store return their graceful defaults ([]/0/None) instead of raising, so a
     scan keeps going.
  4. High-traffic concurrency    — many threads hammering safe_execute over flaky fake
     queries all succeed/degrade with no unhandled exception; reports throughput.
  5. Client singleton under load — (creds only) get_client() is one shared cached client
     across threads (no rebuild race).

Usage:  python scripts/stress_test.py
Exit code 0 = all non-skipped checks passed; nonzero = a failure (CI-usable).
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

import httpx  # noqa: E402

from db import supabase_client as sc  # noqa: E402

_RESULTS: list[tuple[str, str, str]] = []   # (name, status, detail); status in PASS/FAIL/SKIP


def _record(name: str, status: str, detail: str = "") -> None:
    _RESULTS.append((name, status, detail))
    icon = {"PASS": "✅", "FAIL": "❌", "SKIP": "➖"}.get(status, "?")
    print(f"  {icon} {status:4} {name}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# Offline fault-injection doubles: a fake postgrest query builder whose chain
# methods return self and whose .execute() raises N times before returning data.
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, data):
        self.data = data
        self.count = len(data or [])


class _FakeQuery:
    """Doubles as both a client and a query builder — every chain method returns self,
    and .execute() fails `fail_times` times (raising `exc`) before returning `data`."""
    def __init__(self, *, fail_times: int = 0, exc: Exception | None = None, data=None):
        self._fail_times = fail_times
        self._exc = exc or httpx.ConnectError("injected connection failure")
        self._data = data if data is not None else []
        self.calls = 0

    def _self(self, *a, **k):
        return self
    table = select = insert = update = upsert = delete = _self
    eq = ilike = order = limit = lt = gt = neq = _self

    def execute(self):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc
        return _FakeResp(self._data)


def test_safe_execute() -> None:
    print("\n[1] safe_execute retry / backoff")
    # Transient failures below the retry budget → eventually succeeds.
    q = _FakeQuery(fail_times=2, data=[{"x": 1}])
    t0 = time.time()
    try:
        r = sc.safe_execute(q, retries=3)
        ok = (r.data == [{"x": 1}] and q.calls == 3)
        _record("transient errors retried then succeed", "PASS" if ok else "FAIL",
                f"{q.calls} attempts, {time.time()-t0:.1f}s backoff")
    except Exception as exc:
        _record("transient errors retried then succeed", "FAIL", f"raised {exc!r}")

    # Persistent transient failure → re-raises after exhausting the budget (caller catches).
    q = _FakeQuery(fail_times=99)
    try:
        sc.safe_execute(q, retries=3)
        _record("exhausted retries re-raise", "FAIL", "did not raise")
    except Exception as exc:
        ok = sc.is_connectivity_error(exc) and q.calls == 3
        _record("exhausted retries re-raise", "PASS" if ok else "FAIL",
                f"{q.calls} attempts then raised {type(exc).__name__}")

    # Non-transient error → raises IMMEDIATELY, no wasted retries.
    q = _FakeQuery(fail_times=99, exc=ValueError("real bug"))
    try:
        sc.safe_execute(q, retries=3)
        _record("non-transient raises immediately", "FAIL", "did not raise")
    except ValueError:
        _record("non-transient raises immediately", "PASS" if q.calls == 1 else "FAIL",
                f"{q.calls} attempt")
    except Exception as exc:
        _record("non-transient raises immediately", "FAIL", f"wrong type {type(exc).__name__}")


def test_classifier() -> None:
    print("\n[2] is_connectivity_error classification")
    wrapped = RuntimeError("boom")
    wrapped.__cause__ = httpx.ReadTimeout("t")
    cases = [
        ("ConnectError", httpx.ConnectError("x"), True),
        ("wrapped ReadTimeout", wrapped, True),
        ("PoolTimeout", httpx.PoolTimeout("x"), True),
        ("ValueError (real bug)", ValueError("x"), False),
    ]
    for label, exc, expect in cases:
        got = sc.is_connectivity_error(exc)
        _record(f"classify {label}", "PASS" if got == expect else "FAIL",
                f"expected {expect}, got {got}")


def test_scan_touchpoints_degrade() -> None:
    print("\n[3] scan DB touchpoints degrade under a permanent outage")
    from core import seen_ledger, extracted_store
    fake = _FakeQuery(fail_times=999)                 # DB always down
    _orig_sl, _orig_es = seen_ledger.get_client, extracted_store.get_client
    seen_ledger.get_client = lambda: fake
    extracted_store.get_client = lambda: fake
    try:
        try:
            n = seen_ledger.record([{"uid": "T-1", "opportunity_title": "x",
                                     "opportunity_link": "http://x"}])
            _record("seen_ledger.record degrades", "PASS" if not n else "FAIL",
                    f"returned {n!r}, no raise")
        except Exception as exc:
            _record("seen_ledger.record degrades", "FAIL", f"raised {exc!r}")
        try:
            rows = seen_ledger.fetch_all()
            _record("seen_ledger.fetch_all degrades", "PASS" if rows == [] else "FAIL",
                    f"returned {rows!r}")
        except Exception as exc:
            _record("seen_ledger.fetch_all degrades", "FAIL", f"raised {exc!r}")
        try:
            uid = extracted_store.upsert_extracted({"opportunity_url": "http://x",
                                                    "opportunity_name": "X"})
            _record("extracted_store.upsert_extracted degrades",
                    "PASS" if uid is None else "FAIL", f"returned {uid!r}")
        except Exception as exc:
            _record("extracted_store.upsert_extracted degrades", "FAIL", f"raised {exc!r}")
    finally:
        seen_ledger.get_client, extracted_store.get_client = _orig_sl, _orig_es


def test_high_traffic() -> None:
    print("\n[4] high-traffic concurrency over flaky connections")
    # Each thread does a safe_execute over its own fake query that fails a couple of
    # times first — mimicking many simultaneous requests during a flaky-network window.
    n_threads = 64
    errors: list[str] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        q = _FakeQuery(fail_times=(i % 3), data=[{"i": i}])   # 0,1,2 transient failures
        try:
            r = sc.safe_execute(q, retries=4)
            if not (r and r.data and r.data[0]["i"] == i):
                with lock:
                    errors.append(f"thread {i}: bad result")
        except Exception as exc:
            with lock:
                errors.append(f"thread {i}: {type(exc).__name__}")

    t0 = time.time()
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    dt = time.time() - t0
    _record("64 concurrent flaky requests all resolve", "PASS" if not errors else "FAIL",
            f"{n_threads} threads in {dt:.1f}s" + (f"; {errors[:3]}" if errors else ""))


def test_client_singleton() -> None:
    print("\n[5] client singleton under concurrency (needs Supabase creds)")
    if not (sc._read_secret("SUPABASE_URL") and sc._read_secret("SUPABASE_KEY")):
        _record("get_client singleton across threads", "SKIP", "no SUPABASE_URL/KEY set")
        return
    ids: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            c = sc.get_client()             # create_client does not open a socket
            with lock:
                ids.append(id(c))
        except Exception as exc:
            with lock:
                ids.append(-1)
                print(f"      get_client raised: {exc!r}")

    threads = [threading.Thread(target=worker) for _ in range(32)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ok = len(set(ids)) == 1 and ids and ids[0] != -1
    _record("get_client singleton across threads", "PASS" if ok else "FAIL",
            f"{len(set(ids))} distinct instance(s) across 32 threads in {time.time()-t0:.2f}s")


def main() -> int:
    print("=" * 70)
    print("RFPIS resilience stress test")
    print("=" * 70)
    test_safe_execute()
    test_classifier()
    test_scan_touchpoints_degrade()
    test_high_traffic()
    test_client_singleton()

    passed = sum(1 for _, s, _ in _RESULTS if s == "PASS")
    failed = sum(1 for _, s, _ in _RESULTS if s == "FAIL")
    skipped = sum(1 for _, s, _ in _RESULTS if s == "SKIP")
    print("\n" + "=" * 70)
    print(f"RESULT: {passed} passed · {failed} failed · {skipped} skipped")
    print("=" * 70)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
