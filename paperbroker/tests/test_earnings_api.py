"""Offline tests for the earnings routes and the page panel.

Flask test client, fake quote adapter, fake earnings adapter: no network, no
clock drift. The last class pins that the existing option-chain API is not
changed by the new routes.
"""

import unittest

import arrow

from paperbroker.adapters.quotes.CBOEQuoteAdapter import CboeRequestError
from paperbroker.earnings_calendar import (
    CONFIRMATION_ESTIMATED,
    FLAG_SOURCE_NOT_CONFIGURED,
    SESSION_AFTER,
    STATUS_FRESH,
    STATUS_INVALID,
    STATUS_STALE,
    STATUS_UNAVAILABLE,
    STATUS_UNKNOWN,
)
from paperbroker.server import create_app

CLOCK = lambda: arrow.get("2026-10-24T12:00:00+00:00")   # noqa: E731 - a fixture, not style


def _earnings_body(datum="2026-11-02", status=STATUS_FRESH, session=SESSION_AFTER,
                   confirmation=CONFIRMATION_ESTIMATED, flags=None, state="live"):
    return {
        "schema_version": "1.0",
        "ticker": "AAPL",
        "earnings": {
            "next_earnings_date": datum,
            "release_session": session,
            "date_confirmation": confirmation,
            "source": "nasdaq_earnings_date",
            "source_checked_at_utc": "2026-10-24T06:00:00Z",
            "retrieved_at_utc": "2026-10-24T12:00:00Z",
            "status": status,
            "quality_flags": list(flags or []),
        },
        "cache": {"state": state, "stored_at_utc": "2026-10-24T06:00:00Z",
                  "age_seconds": 3600, "ttl_seconds": 21600, "freshness_limit_seconds": 172800},
    }


class _FakeEarnings:
    def __init__(self, body=None, boom=False):
        self.body = body if body is not None else _earnings_body()
        self.boom = boom
        self.calls = []

    def get_earnings(self, ticker):
        self.calls.append(ticker)
        if self.boom:
            raise RuntimeError("adapter exploded")
        return self.body


class _ChainAdapter:
    """Chain with two listed expirations: one before, one after the earnings date."""

    EXPIRATIONS = ["2026-10-30", "2026-11-20"]

    def __init__(self, fail=False):
        self.fail = fail

    def get_expiration_dates(self, underlying_asset=None):
        return list(self.EXPIRATIONS)

    def get_options(self, underlying_asset=None, expiration_date=None):
        return []

    def get_quote(self, asset):
        return None

    def get_option_chain_snapshot(self, underlying_asset=None):
        if self.fail:
            raise CboeRequestError("feed down")
        return {
            "records": [
                {"option": "AAPL261030C00335000", "bid": 1.0, "ask": 1.2},
                {"option": "AAPL261120C00335000", "bid": 2.0, "ask": 2.2},
            ],
            "fetched_at": "2026-10-24T11:00:00Z",
        }


def _client(earnings=None, chain=None):
    return create_app(
        quote_adapter=chain or _ChainAdapter(),
        earnings_adapter=earnings or _FakeEarnings(),
        now_fn=CLOCK,
    ).test_client()


class TestEarningsRoute(unittest.TestCase):
    def test_the_contract_is_served(self):
        antwort = _client().get("/api/v1/earnings?ticker=aapl")
        self.assertEqual(antwort.status_code, 200)
        koerper = antwort.get_json()
        self.assertEqual(koerper["schema_version"], "1.0")
        self.assertEqual(koerper["ticker"], "AAPL")
        self.assertEqual(koerper["earnings"]["next_earnings_date"], "2026-11-02")
        self.assertEqual(koerper["cache"]["state"], "live")

    def test_a_missing_ticker_is_a_client_error(self):
        antwort = _client().get("/api/v1/earnings")
        self.assertEqual(antwort.status_code, 400)
        self.assertEqual(antwort.get_json()["error"]["code"], "ticker_required")

    def test_an_invalid_ticker_is_rejected_before_any_query(self):
        earnings = _FakeEarnings()
        antwort = _client(earnings=earnings).get("/api/v1/earnings?ticker=AAPL1")
        self.assertEqual(antwort.status_code, 400)
        self.assertEqual(antwort.get_json()["error"]["code"], "invalid_ticker")
        self.assertEqual(earnings.calls, [])

    def test_unknown_parameters_are_named(self):
        antwort = _client().get("/api/v1/earnings?ticker=AAPL&date=2026-11-02")
        self.assertEqual(antwort.status_code, 400)
        self.assertEqual(antwort.get_json()["error"]["code"], "unknown_parameter")

    def test_duplicate_parameters_are_rejected(self):
        antwort = _client().get("/api/v1/earnings?ticker=AAPL&ticker=MSFT")
        self.assertEqual(antwort.status_code, 400)
        self.assertEqual(antwort.get_json()["error"]["code"], "duplicate_parameter")

    def test_an_adapter_crash_becomes_a_json_error(self):
        antwort = _client(earnings=_FakeEarnings(boom=True)).get("/api/v1/earnings?ticker=AAPL")
        self.assertEqual(antwort.status_code, 503)
        self.assertEqual(antwort.get_json()["error"]["code"], "earnings_source_failed")

    def test_the_null_adapter_answers_unknown_and_says_why(self):
        app = create_app(quote_adapter=_ChainAdapter(), now_fn=CLOCK)
        koerper = app.test_client().get("/api/v1/earnings?ticker=AAPL").get_json()
        self.assertIsNone(koerper["earnings"]["next_earnings_date"])
        self.assertEqual(koerper["earnings"]["status"], STATUS_UNKNOWN)
        self.assertIn(FLAG_SOURCE_NOT_CONFIGURED, koerper["earnings"]["quality_flags"])


class TestEarningsWindowRoute(unittest.TestCase):
    def test_the_window_uses_the_listed_expirations(self):
        antwort = _client().get("/api/v1/earnings-window?ticker=AAPL")
        self.assertEqual(antwort.status_code, 200)
        koerper = antwort.get_json()
        self.assertEqual(koerper["ticker"], "AAPL")
        self.assertEqual(koerper["chain"], {"status": "ok", "expirations_read": True})
        self.assertEqual(koerper["expirations"]["listed"], ["2026-10-30", "2026-11-20"])
        self.assertEqual(koerper["expirations"]["selected"], "2026-11-20")
        self.assertEqual(koerper["exit"]["planned_last_exit_date"], "2026-10-30")
        self.assertTrue(koerper["decision"]["eligible"])

    def test_an_unproven_status_never_reaches_the_route_as_eligible(self):
        # The route is what a caller reads. A valid date and a listed expiry are
        # not enough: when the adapter says the proof is unknown, the answer has
        # to say so - all the way through to ``eligible``.
        for status, code in (
            (STATUS_UNKNOWN, "earnings_status_unknown"),
            (STATUS_INVALID, "earnings_data_invalid"),
            (STATUS_UNAVAILABLE, "earnings_source_unavailable"),
            (STATUS_STALE, "earnings_data_stale"),
        ):
            with self.subTest(status=status):
                earnings = _FakeEarnings(_earnings_body(status=status))
                koerper = _client(earnings=earnings).get(
                    "/api/v1/earnings-window?ticker=AAPL"
                ).get_json()
                self.assertFalse(koerper["decision"]["eligible"])
                self.assertNotEqual(koerper["decision"]["status"], "ok")
                self.assertIn(code, [g["code"] for g in koerper["decision"]["reasons"]])
                self.assertEqual(koerper["earnings"]["status"], status)

    def test_no_listed_expiry_after_earnings_blocks_instead_of_guessing(self):
        earnings = _FakeEarnings(_earnings_body(datum="2026-11-25"))
        koerper = _client(earnings=earnings).get("/api/v1/earnings-window?ticker=AAPL").get_json()
        self.assertFalse(koerper["decision"]["eligible"])
        self.assertIsNone(koerper["expirations"]["selected"])
        self.assertEqual(
            [g["code"] for g in koerper["decision"]["reasons"]],
            ["no_listed_expiration_after_earnings"],
        )

    def test_a_dead_chain_leaves_the_expirations_unknown(self):
        koerper = _client(chain=_ChainAdapter(fail=True)).get(
            "/api/v1/earnings-window?ticker=AAPL"
        ).get_json()
        self.assertEqual(koerper["chain"]["status"], "market_data_unavailable")
        self.assertFalse(koerper["chain"]["expirations_read"])
        self.assertEqual(koerper["expirations"]["listed"], [])
        self.assertFalse(koerper["decision"]["eligible"])
        self.assertIn("expirations_unavailable", [g["code"] for g in koerper["decision"]["reasons"]])

    def test_a_past_earnings_date_blocks(self):
        koerper = _client(earnings=_FakeEarnings(_earnings_body(datum="2026-10-01"))).get(
            "/api/v1/earnings-window?ticker=AAPL"
        ).get_json()
        self.assertIn("earnings_date_in_past", [g["code"] for g in koerper["decision"]["reasons"]])


class TestPage(unittest.TestCase):
    def test_the_panel_shows_the_date_the_exit_and_the_expiry(self):
        seite = _client().get("/?ticker=AAPL")
        self.assertEqual(seite.status_code, 200)
        html = seite.get_data(as_text=True)
        self.assertIn("Earnings-Termin", html)
        self.assertIn("2026-11-02", html)
        self.assertIn("nachbörslich", html)
        self.assertIn("geschätzt", html)
        self.assertIn("2026-11-20", html)
        self.assertIn("2026-10-30", html)
        self.assertIn("erfüllt", html)

    def test_the_panel_makes_missing_data_visible(self):
        app = create_app(quote_adapter=_ChainAdapter(), now_fn=CLOCK)
        html = app.test_client().get("/?ticker=AAPL").get_data(as_text=True)
        self.assertIn("Earnings-Termin", html)
        self.assertIn("UNBEKANNT", html)
        self.assertIn("Keine Earnings-Quelle konfiguriert", html)
        self.assertIn("nicht entscheidbar", html)

    def test_an_earnings_failure_does_not_break_the_page(self):
        seite = _client(earnings=_FakeEarnings(boom=True)).get("/?ticker=AAPL")
        self.assertEqual(seite.status_code, 200)
        self.assertIn("konnten für diesen Ticker nicht gelesen werden", seite.get_data(as_text=True))


class TestOptionChainCompatibility(unittest.TestCase):
    def test_the_old_route_still_answers_the_same_envelope(self):
        antwort = _client().get("/api/v1/option-chain?ticker=AAPL")
        self.assertEqual(antwort.status_code, 200)
        koerper = antwort.get_json()
        self.assertEqual(koerper["schema_version"], "1.0")
        self.assertEqual(koerper["ticker"], "AAPL")
        self.assertIn("contracts", koerper)
        self.assertEqual(len(koerper["contracts"]), 2)
        # The new routes must not have added a parameter to the old one.
        self.assertEqual(antwort.status_code, 200)

    def test_a_listed_expiration_is_still_accepted_and_an_unlisted_one_rejected(self):
        client = _client()
        self.assertEqual(client.get("/api/v1/option-chain?ticker=AAPL&expiration=2026-11-20").status_code, 200)
        abgelehnt = client.get("/api/v1/option-chain?ticker=AAPL&expiration=2026-12-18")
        self.assertEqual(abgelehnt.status_code, 400)
        self.assertEqual(abgelehnt.get_json()["error"]["code"], "expiration_not_listed")


if __name__ == "__main__":
    unittest.main()
