"""The shared crawl session must identify itself on every request.

`core.http` set no User-Agent, so any fetch that did not pass one explicitly
went out as `python-requests/2.x` - which a number of donor hosts refuse.
Measured on the same URLs in the same second:

    mesamalaria.org   no UA -> 403 (146 bytes)    this UA -> 200 (274,105 bytes)
    idrc-crdi.ca      no UA -> 403  (93 bytes)    this UA -> 200  (51,577 bytes)

The failure was invisible. A 403 body is short but non-empty, so extraction
"succeeded" with no deadline, no posted date and no closure text, and every
downstream gate then kept the row, because none of them can reject on absence of
evidence. Three of the nine undated Decline rows in one review week came in this
way, and so did a call whose eligible-country list was sitting unread on its page.

These tests are offline. They assert what the session will SEND, using requests'
own header-merging via prepare_request, rather than making a network call.
"""
from __future__ import annotations

import unittest

import requests

import core.http as H


class TheSessionIdentifiesItself(unittest.TestCase):
    def test_a_user_agent_is_set_on_the_shared_session(self):
        ua = H._session.headers.get("User-Agent")
        self.assertTrue(ua)
        self.assertNotIn("python-requests", ua.lower())

    def test_the_user_agent_names_the_application_and_a_contact(self):
        # Self-identifying on purpose: measured as sufficient, so there is no
        # reason to pose as a browser.
        self.assertIn("RFPIS", H.USER_AGENT)
        self.assertIn("contact", H.USER_AGENT.lower())

    def test_a_request_with_no_headers_still_sends_it(self):
        req = requests.Request("GET", "https://donor.example/call")
        prepared = H._session.prepare_request(req)
        self.assertEqual(prepared.headers.get("User-Agent"), H.USER_AGENT)

    def test_accept_headers_are_sent_too(self):
        req = requests.Request("GET", "https://donor.example/call")
        prepared = H._session.prepare_request(req)
        self.assertIn("text/html", prepared.headers.get("Accept", ""))
        self.assertTrue(prepared.headers.get("Accept-Language"))


class CallSitesCanStillOverride(unittest.TestCase):
    """~30 call sites in core.scraper pass their own headers (e.g. Accept: PDF)."""

    def test_an_explicit_user_agent_wins(self):
        req = requests.Request("GET", "https://donor.example/call",
                               headers={"User-Agent": "Something/9.9"})
        prepared = H._session.prepare_request(req)
        self.assertEqual(prepared.headers.get("User-Agent"), "Something/9.9")

    def test_an_explicit_accept_wins_and_the_ua_survives(self):
        req = requests.Request("GET", "https://donor.example/guidance.pdf",
                               headers={"Accept": "application/pdf, */*"})
        prepared = H._session.prepare_request(req)
        self.assertEqual(prepared.headers.get("Accept"), "application/pdf, */*")
        self.assertEqual(prepared.headers.get("User-Agent"), H.USER_AGENT)


class OneDefinition(unittest.TestCase):
    """core.scraper re-exports the constant instead of defining its own.

    Two definitions of the crawler's identity can drift, and the one that
    matters is whatever the shared session actually sends.
    """

    def test_scraper_uses_the_same_object(self):
        from core.scraper import USER_AGENT as scraper_ua
        self.assertIs(scraper_ua, H.USER_AGENT)

    def test_the_page_monitor_keeps_its_own_identity(self):
        # Deliberately separate: it is a different crawler with its own contact,
        # and collapsing them would misreport which process hit a host.
        from core.page_monitor import USER_AGENT as monitor_ua
        self.assertNotEqual(monitor_ua, H.USER_AGENT)
        self.assertIn("PageMonitor", monitor_ua)


class ContactAddressIsConfigurable(unittest.TestCase):
    def test_the_contact_comes_from_the_environment_with_a_neutral_default(self):
        # The default must be a reserved example domain, so no real organisation's
        # address is baked into the source and shipped to every host we crawl.
        # Deployments set SCRAPER_CONTACT_EMAIL to a real one.
        import os
        if os.environ.get("SCRAPER_CONTACT_EMAIL"):
            self.skipTest("a real contact address is configured in this environment")
        self.assertIn("example.org", H.USER_AGENT.lower())


if __name__ == "__main__":
    unittest.main()
