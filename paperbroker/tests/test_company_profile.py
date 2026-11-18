"""``company_profile.web_url``: one lookup, and every failure is ``None``.

Pinned here:

* no key -> ``None`` and no request at all,
* only ``https`` becomes a link: ``http``, ``javascript:``, ``data:``, a URL
  without a host and a URL with credentials are all refused,
* an unknown ticker, a refused answer and an unreadable body are states, not
  exceptions - the detail page must never turn them into a 500,
* the answer is cached, and a cached ``None`` also costs no second request.
"""

import unittest
from unittest import mock

from paperbroker import company_profile as cp

NOW = [1000.0]


def _clock():
    return NOW[0]


class _Answer:
    def __init__(self, status_code=200, payload=None, raise_exc=None):
        self.status_code = status_code
        self._payload = payload
        self._raise = raise_exc

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Session:
    """Records every call and answers with one prepared response."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def get(self, url, params=None, timeout=None, verify=None):
        # ``verify`` wird mitgeschrieben: die Zusage "Zertifikat wird geprueft"
        # gehoert geprueft, nicht geglaubt.
        self.calls.append((url, dict(params or {}), verify))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class Base(unittest.TestCase):
    def setUp(self):
        cp.clear_cache()
        NOW[0] = 1000.0


class UsableUrlTest(unittest.TestCase):
    def test_https_with_a_host_passes(self):
        self.assertEqual("https://www.corteva.com/",
                         cp.usable_url("https://www.corteva.com/"))
        self.assertEqual("https://x.example/a?b=1",
                         cp.usable_url("  https://x.example/a?b=1  "))

    def test_everything_else_is_nothing(self):
        for wert in (None, "", "   ", "http://example.com/", "javascript:alert(1)",
                     "data:text/html,x", "https://", "https:///pfad",
                     "https://user:pw@example.com/", 42):
            with self.subTest(wert=wert):
                self.assertIsNone(cp.usable_url(wert))


class NoKeyTest(Base):
    def test_without_a_key_nothing_is_asked(self):
        with mock.patch.dict("os.environ", {cp.ENV_API_KEY: ""}):
            session = _Session(_Answer(payload={"weburl": "https://a.example/"}))
            self.assertIsNone(cp.web_url("CTVA", session=session, now_fn=_clock))
        self.assertEqual([], session.calls)


class LookupTest(Base):
    def test_the_website_of_the_answer_is_returned(self):
        session = _Session(_Answer(payload={"weburl": "https://www.corteva.com/"}))
        self.assertEqual("https://www.corteva.com/",
                         cp.web_url("ctva", token="k", session=session, now_fn=_clock))
        url, params, verify = session.calls[0]
        self.assertEqual(cp.PROFILE_URL, url)
        self.assertEqual({"symbol": "CTVA", "token": "k"}, params,
                         "das Symbol geht in Grossbuchstaben hinaus")
        self.assertIs(True, verify, "das Zertifikat wird ausdruecklich geprueft")

    def test_a_second_call_is_answered_from_the_cache(self):
        session = _Session(_Answer(payload={"weburl": "https://a.example/"}))
        for _ in range(3):
            cp.web_url("CTVA", token="k", session=session, now_fn=_clock)
        self.assertEqual(1, len(session.calls), "eine Abfrage, drei Antworten")

    def test_the_cache_expires(self):
        session = _Session(_Answer(payload={"weburl": "https://a.example/"}))
        cp.web_url("CTVA", token="k", session=session, now_fn=_clock)
        NOW[0] += cp.DEFAULT_TTL_SECONDS + 1
        cp.web_url("CTVA", token="k", session=session, now_fn=_clock)
        self.assertEqual(2, len(session.calls))

    def test_an_unknown_ticker_is_cached_too(self):
        # Eine 200 ohne weburl ist eine Antwort: sie fuer jeden Klick erneut zu
        # holen waere eine Anfrage je Detailaufruf.
        session = _Session(_Answer(payload={"name": "X"}))
        self.assertIsNone(cp.web_url("ZZZZ", token="k", session=session, now_fn=_clock))
        self.assertIsNone(cp.web_url("ZZZZ", token="k", session=session, now_fn=_clock))
        self.assertEqual(1, len(session.calls))


class FailureTest(Base):
    def test_a_failed_lookup_is_cached_briefly_and_then_retried(self):
        # Kody-Befund: ohne Zwischenspeicher fragt jeder Klick den Dienst erneut
        # (4 s im Handler). Der Fehlschlag wird deshalb gemerkt - aber kurz:
        # nach FAILURE_TTL_SECONDS darf ein zweiter Versuch wieder hinaus.
        session = _Session(_Answer(status_code=429, payload={}))
        with self.assertLogs(cp.logger, level="WARNING"):
            cp.web_url("CTVA", token="k", session=session, now_fn=_clock)
            cp.web_url("CTVA", token="k", session=session, now_fn=_clock)
        self.assertEqual(1, len(session.calls), "ein Fehlschlag, ein Aufruf")

        NOW[0] += cp.FAILURE_TTL_SECONDS + 1
        session.answer = _Answer(payload={"weburl": "https://a.example/"})
        self.assertEqual("https://a.example/",
                         cp.web_url("CTVA", token="k", session=session, now_fn=_clock))
        self.assertEqual(2, len(session.calls), "nach der kurzen Frist neu gefragt")

    def test_a_refused_answer_is_none(self):
        session = _Session(_Answer(status_code=403, payload={}))
        with self.assertLogs(cp.logger, level="WARNING"):
            self.assertIsNone(cp.web_url("CTVA", token="k", session=session, now_fn=_clock))

    def test_an_unreadable_body_is_none(self):
        session = _Session(_Answer(payload=ValueError("kein json")))
        with self.assertLogs(cp.logger, level="WARNING"):
            self.assertIsNone(cp.web_url("CTVA", token="k", session=session, now_fn=_clock))

    def test_a_transport_error_is_none_and_not_an_exception(self):
        session = _Session(ConnectionError("weg"))
        with self.assertLogs(cp.logger, level="WARNING"):
            self.assertIsNone(cp.web_url("CTVA", token="k", session=session, now_fn=_clock))

    def test_a_payload_that_is_no_object_is_none(self):
        session = _Session(_Answer(payload=["kein", "objekt"]))
        self.assertIsNone(cp.web_url("CTVA", token="k", session=session, now_fn=_clock))

    def test_a_plain_http_website_is_refused(self):
        session = _Session(_Answer(payload={"weburl": "http://example.com/"}))
        with self.assertLogs(cp.logger, level="INFO"):
            self.assertIsNone(cp.web_url("CTVA", token="k", session=session, now_fn=_clock))


if __name__ == "__main__":
    unittest.main()
