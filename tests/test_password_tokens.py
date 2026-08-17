"""One-time account-setup links: minted once, single-use, expiring, hash-only at rest.

This flow replaced emailing a plaintext temporary password. It shipped without tests, and
then never reached `main` at all: its PR was opened against another FEATURE BRANCH as its
base rather than against `main`, and when that base was squash-merged the later content was
stranded. So the behaviour the owner was looking for - a clickable link instead of a
password in the inbox - was merged on GitHub and absent from the running app.

What is covered here is the part worth guarding: the security properties.

  * the raw token is returned ONCE and only its sha256 is stored
  * a link is single-use, and two tabs racing cannot both spend it
  * an expired link is refused
  * issuing a new link retires the outstanding one, so a re-sent invite does not leave two
    working ways in
  * an unknown, used and expired token are indistinguishable to the caller - saying "that
    link expired" confirms the link once existed

Run:  python -m unittest tests.test_password_tokens
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import password_tokens as PT                                # noqa: E402

USER = "11111111-2222-3333-4444-555555555555"


class _Table:
    """An in-memory stand-in for the two tables this module touches."""

    def __init__(self, store, name):
        self.store, self.name = store, name
        self._filters, self._payload, self._op = [], None, None

    # -- builder ----------------------------------------------------------
    def select(self, *_a, **_k):
        self._op = "select"
        return self

    def insert(self, row, *_a, **_k):
        self._op, self._payload = "insert", row
        return self

    def update(self, values, *_a, **_k):
        self._op, self._payload = "update", values
        return self

    def eq(self, col, val):
        self._filters.append((col, "eq", val))
        return self

    def is_(self, col, val):
        self._filters.append((col, "is", val))
        return self

    def limit(self, _n):
        return self

    # -- execution --------------------------------------------------------
    def _matches(self, row):
        for col, op, val in self._filters:
            if op == "eq" and row.get(col) != val:
                return False
            if op == "is" and row.get(col) is not None:
                return False
        return True

    def execute(self):
        rows = self.store.setdefault(self.name, [])
        if self._op == "insert":
            rec = dict(self._payload)
            rec.setdefault("id", "tok-%d" % (len(rows) + 1))
            rec.setdefault("used_at", None)
            rows.append(rec)
            return type("R", (), {"data": [rec]})()
        hit = [r for r in rows if self._matches(r)]
        if self._op == "update":
            for r in hit:
                r.update(self._payload)
        return type("R", (), {"data": hit})()


class _Client:
    def __init__(self, store):
        self.store = store

    def table(self, name):
        return _Table(self.store, name)


class _Base(unittest.TestCase):
    def setUp(self):
        self.store = {"users": [{"id": USER, "email": "a@example.org",
                                 "name": "A Person", "is_active": True}],
                      "user_password_tokens": []}
        self._orig = PT.service_client
        PT.service_client = lambda: _Client(self.store)
        self.addCleanup(lambda: setattr(PT, "service_client", self._orig))

    @property
    def tokens(self):
        return self.store["user_password_tokens"]


class TheTokenIsNotStoredTests(_Base):
    def test_only_the_digest_is_written(self):
        raw, _ = PT.issue_token(user_id=USER, purpose=PT.PURPOSE_INVITE)
        row = self.tokens[-1]
        self.assertEqual(row["token_hash"], PT._digest(raw))
        self.assertNotIn(raw, str(row),
                         "the raw token must never be persisted anywhere")

    def test_two_tokens_are_never_the_same(self):
        a, _ = PT.issue_token(user_id=USER, purpose=PT.PURPOSE_INVITE)
        b, _ = PT.issue_token(user_id=USER, purpose=PT.PURPOSE_INVITE)
        self.assertNotEqual(a, b)

    def test_an_invite_lasts_longer_than_a_reset(self):
        # A new joiner may not read email today; a reset is acted on at once, so a
        # shorter window limits how long a forwarded copy stays live.
        self.assertGreater(PT._TTL[PT.PURPOSE_INVITE], PT._TTL[PT.PURPOSE_RESET])

    def test_an_unknown_purpose_is_refused(self):
        with self.assertRaises(ValueError):
            PT.issue_token(user_id=USER, purpose="something-else")


class ALinkWorksOnceTests(_Base):
    def test_a_fresh_token_validates_and_carries_the_user(self):
        raw, _ = PT.issue_token(user_id=USER, purpose=PT.PURPOSE_INVITE)
        rec = PT.peek_token(raw)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["user"]["email"], "a@example.org")

    def test_peeking_does_not_spend_it(self):
        # The commonest thing a confused user does is refresh the page.
        raw, _ = PT.issue_token(user_id=USER, purpose=PT.PURPOSE_INVITE)
        self.assertIsNotNone(PT.peek_token(raw))
        self.assertIsNotNone(PT.peek_token(raw))

    def test_consuming_spends_it(self):
        raw, _ = PT.issue_token(user_id=USER, purpose=PT.PURPOSE_INVITE)
        self.assertIsNotNone(PT.consume_token(raw))
        self.assertIsNone(PT.peek_token(raw))

    def test_two_tabs_racing_cannot_both_spend_it(self):
        raw, _ = PT.issue_token(user_id=USER, purpose=PT.PURPOSE_INVITE)
        self.assertIsNotNone(PT.consume_token(raw))
        self.assertIsNone(PT.consume_token(raw))


class ExpiryAndSupersedingTests(_Base):
    def test_an_expired_link_is_refused(self):
        raw, _ = PT.issue_token(user_id=USER, purpose=PT.PURPOSE_RESET)
        self.tokens[-1]["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        self.assertIsNone(PT.peek_token(raw))

    def test_issuing_a_new_link_retires_the_old_one(self):
        first, _ = PT.issue_token(user_id=USER, purpose=PT.PURPOSE_INVITE)
        second, _ = PT.issue_token(user_id=USER, purpose=PT.PURPOSE_INVITE)
        self.assertIsNone(PT.peek_token(first), "a re-sent invite must retire the first")
        self.assertIsNotNone(PT.peek_token(second))

    def test_setting_a_password_retires_every_outstanding_link(self):
        raw, _ = PT.issue_token(user_id=USER, purpose=PT.PURPOSE_RESET)
        PT.invalidate_tokens_for_user(USER)
        self.assertIsNone(PT.peek_token(raw))


class NothingIsRevealedTests(_Base):
    def test_unknown_used_and_expired_all_look_the_same(self):
        # Distinguishing them tells a stranger the link once existed.
        used, _ = PT.issue_token(user_id=USER, purpose=PT.PURPOSE_INVITE)
        PT.consume_token(used)
        expired, _ = PT.issue_token(user_id=USER, purpose=PT.PURPOSE_RESET)
        self.tokens[-1]["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        for tok in ("never-existed", used, expired, "", None):
            self.assertIsNone(PT.peek_token(tok))

    def test_a_token_for_a_deleted_user_is_refused(self):
        raw, _ = PT.issue_token(user_id=USER, purpose=PT.PURPOSE_INVITE)
        self.store["users"].clear()
        self.assertIsNone(PT.peek_token(raw))


class TheLinkItselfTests(unittest.TestCase):
    def test_it_is_built_from_the_deployment_url(self):
        self.assertEqual(PT.build_link("https://example.org", "abc"),
                         "https://example.org/?token=abc")

    def test_a_trailing_slash_does_not_double_up(self):
        self.assertEqual(PT.build_link("https://example.org/", "abc"),
                         "https://example.org/?token=abc")


class TheLinkOriginTests(unittest.TestCase):
    """The link is only as good as the host it points at."""

    def test_the_app_url_prefers_a_configured_setting(self):
        # It read env ONLY, and with APP_PUBLIC_URL unset every invite link pointed at
        # localhost - a link nobody outside the developer's machine can use, where a
        # temporary password at least worked.
        from core import user_emails as UE
        import core.settings as S
        orig = S.get_setting
        S.get_setting = lambda k, d=None: ("https://app.example.org"
                                           if k == "app_public_url" else d)
        try:
            self.assertEqual(UE._app_url(), "https://app.example.org")
        finally:
            S.get_setting = orig

    def test_it_still_falls_back_to_env_then_localhost(self):
        from core import user_emails as UE
        import core.settings as S
        orig, orig_env = S.get_setting, os.environ.get("APP_PUBLIC_URL")
        S.get_setting = lambda k, d=None: d
        os.environ["APP_PUBLIC_URL"] = "https://from-env.example.org"
        try:
            self.assertEqual(UE._app_url(), "https://from-env.example.org")
            del os.environ["APP_PUBLIC_URL"]
            self.assertEqual(UE._app_url(), "http://localhost:8501")
        finally:
            S.get_setting = orig
            if orig_env is not None:
                os.environ["APP_PUBLIC_URL"] = orig_env
            else:
                os.environ.pop("APP_PUBLIC_URL", None)


class TheEmailIsFramedAsActivationTests(unittest.TestCase):
    """What the recipient is asked to do is turn on an account (owner, 2026-08-17).

    Choosing a password is how that happens, not the point of the message. "Set up your
    account" also reads like configuration work, and "Set my password" invites "which
    password?" from somebody who has never had one.
    """

    def _sent(self, fn, **kw):
        from core import user_emails as UE
        captured = {}

        def _fake(to, subject, html):
            captured.update(to=to, subject=subject, html=html)
            return {"id": "x"}

        orig = UE.send_email
        UE.send_email = _fake
        try:
            fn(**kw)
        finally:
            UE.send_email = orig
        return captured

    def test_the_invite_says_activate(self):
        from core import user_emails as UE
        got = self._sent(UE.send_welcome_email, to_email="a@example.org",
                         to_name="A", setup_link="https://x.example/?token=t")
        self.assertIn("Activate your", got["subject"])
        self.assertIn("Activate account", got["html"])
        self.assertNotIn("Set my password", got["html"])
        self.assertNotIn("temporary password", got["html"].lower())

    def test_the_invite_still_carries_the_link_and_no_password(self):
        from core import user_emails as UE
        got = self._sent(UE.send_welcome_email, to_email="a@example.org",
                         to_name="A", setup_link="https://x.example/?token=SECRET")
        self.assertIn("https://x.example/?token=SECRET", got["html"])

    def test_the_invite_prints_the_token_as_a_pasteable_code(self):
        # A link is not a reliable carrier: a hosted Streamlit app bootstraps a session
        # before serving a cold request, and the round trip DROPS THE QUERY STRING - so the
        # one visitor the link was written for is exactly the one it fails for. Measured on
        # the live deployment: /?token=ABC -> /-/auth/app?redirect_uri=<app>/ (no token).
        from core import user_emails as UE
        got = self._sent(UE.send_welcome_email, to_email="a@example.org", to_name="A",
                         setup_link="https://app.example/?token=TOKEN123abc")
        self.assertIn("TOKEN123abc", got["html"])
        self.assertIn("activation or reset code", got["html"])

    def test_the_reset_prints_its_code_too(self):
        from core import user_emails as UE
        got = self._sent(UE.send_password_reset_email, to_email="a@example.org",
                         to_name="A", reset_link="https://app.example/?token=RESET9xy")
        self.assertIn("RESET9xy", got["html"])

    def test_a_link_without_a_token_prints_no_code_block(self):
        from core import user_emails as UE
        self.assertEqual(UE._code_fallback("https://app.example/"), "")

    def test_the_reset_says_change_your_password(self):
        from core import user_emails as UE
        got = self._sent(UE.send_password_reset_email, to_email="a@example.org",
                         to_name="A", reset_link="https://x.example/?token=t")
        self.assertIn("Change your", got["subject"])
        self.assertIn("Change my password", got["html"])


class TheMigrationTests(unittest.TestCase):
    def test_it_is_idempotent_and_stores_only_a_hash(self):
        # Migrations here are applied BY HAND, so re-running one must be safe.
        import io
        path = os.path.join(_ROOT, "db", "migrations",
                            "093_user_password_tokens.sql")
        with io.open(path, encoding="utf-8") as fh:
            sql = fh.read().lower()
        self.assertIn("create table if not exists user_password_tokens", sql)
        self.assertIn("token_hash", sql)
        self.assertNotIn("token text", sql)          # never the token itself
        self.assertIn("on delete cascade", sql)      # tokens die with the account
        self.assertEqual(sql.count("create index if not exists"), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
