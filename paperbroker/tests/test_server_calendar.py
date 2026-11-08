"""Offline tests for the earnings calendar block on the start page (plan C1-C6).

``test_server.py`` stays untouched: this file owns the new block only. The
calendar adapter is faked, so no test here needs the network, and every test
runs against the pinned clock.
"""

import re
import unittest

import arrow

from paperbroker.adapters.earnings.NasdaqEarningsAdapter import NullEarningsAdapter
from paperbroker.server import create_app

# 2026-11-08 12:00 UTC is 07:00 in New York - the US calendar day is still the 8th.
CLOCK = lambda: arrow.get("2026-11-08T12:00:00Z")  # noqa: E731

STAMP = "2026-11-08T06:00:00Z"


def _row(symbol, cap=1000, session="before_market", eps="$1.41"):
    return {
        "symbol": symbol,
        "name": f"{symbol} Corporation",
        "session": session,
        "eps_forecast": eps,
        "market_cap": cap,
        "fiscal_quarter_ending": "Sep/2026",
    }


class _FakeCalendar:
    """A calendar adapter that answers from memory and counts its calls."""

    def __init__(self, rows_by_day=None, state="live", boom=False):
        self.rows_by_day = rows_by_day or {}
        self.state = state
        self.boom = boom
        self.calls = 0
        self.last_days = None

    def get_calendar_days(self, days):
        self.calls += 1
        self.last_days = dict(days)
        if self.boom:
            raise RuntimeError("calendar adapter exploded")
        geliefert = {}
        for _label, tag in days.items():
            geliefert[tag] = {
                "date": tag,
                "rows": list(self.rows_by_day.get(tag, [])),
                "state": self.state,
                "fetched_at_utc": STAMP,
                "payload_ok": True,
                "skipped": 0,
            }
        return {"days": geliefert, "state": self.state, "fetched_at_utc": STAMP}


class _Chain:
    """Two listed expirations inside the default 7-45 DTE window."""

    EXPIRATIONS = ["2026-11-20", "2026-12-18"]

    def get_expiration_dates(self, underlying_asset=None):
        return list(self.EXPIRATIONS)

    def get_options(self, underlying_asset=None, expiration_date=None):
        return []

    def get_quote(self, asset):
        return None


def _client(calendar=None, chain=None):
    return create_app(
        quote_adapter=chain or _Chain(),
        earnings_adapter=calendar or _FakeCalendar(),
        now_fn=CLOCK,
    ).test_client()


def _select_block(html):
    treffer = re.search(r'<select name="expiration".*?</select>', html, re.S)
    assert treffer is not None, "expiration select missing"
    return treffer.group(0)


class TestCalendarBlock(unittest.TestCase):
    def test_the_start_page_shows_three_groups_with_dates(self):
        # C1
        kalender = _FakeCalendar({
            "2026-11-07": [_row("AAA")],
            "2026-11-08": [_row("BBB")],
            "2026-11-09": [_row("CCC")],
        })
        antwort = _client(kalender).get("/")
        html = antwort.get_data(as_text=True)
        self.assertEqual(antwort.status_code, 200)
        self.assertIn("Earnings-Kalender", html)
        self.assertEqual(html.count('class="cal-group"'), 3)
        for tag in ("07.11.2026", "08.11.2026", "09.11.2026"):
            self.assertIn(tag, html)
        for label in ("gestern", "heute", "morgen"):
            self.assertIn(label, html)

    def test_a_row_links_to_the_start_page_with_the_ticker(self):
        # C2
        kalender = _FakeCalendar({"2026-11-08": [_row("AAPL")]})
        html = _client(kalender).get("/").get_data(as_text=True)
        self.assertIn('href="/?ticker=AAPL#screener-form"', html)
        self.assertIn('id="screener-form"', html)
        self.assertNotIn("/ticker/AAPL", html)

    def test_only_the_top_25_are_linked_and_the_total_is_named(self):
        # C3
        rows = [_row(f"{chr(65 + i // 26)}{chr(65 + i % 26)}", cap=(40 - i) * 1000) for i in range(40)]
        kalender = _FakeCalendar({"2026-11-08": rows})
        html = _client(kalender).get("/").get_data(as_text=True)
        self.assertEqual(html.count('href="/?ticker='), 25)
        self.assertIn("25 von 40", html)

    def test_a_broken_adapter_still_renders_the_screener(self):
        # C4
        kalender = _FakeCalendar(boom=True)
        antwort = _client(kalender).get("/")
        html = antwort.get_data(as_text=True)
        self.assertEqual(antwort.status_code, 200)
        self.assertIn("Kalender nicht verfügbar", html)
        self.assertIn("Instrument", html)
        self.assertEqual(html.count('class="cal-row"'), 0)

    def test_healthz_never_touches_the_calendar(self):
        # C5
        kalender = _FakeCalendar({"2026-11-08": [_row("AAPL")]})
        client = _client(kalender)
        antwort = client.get("/healthz")
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(antwort.get_json(), {"status": "ok"})
        self.assertEqual(kalender.calls, 0)

    def test_a_ticker_request_keeps_the_expiration_list_unchanged(self):
        # C6
        client = _client()
        mit = client.get("/?ticker=SYM").get_data(as_text=True)
        ohne = client.get("/?ticker=SYM&no_calendar=1").get_data(as_text=True)
        self.assertIn('value="SYM"', mit)
        self.assertIn("2026-11-20", mit)
        self.assertIn("2026-12-18", mit)
        self.assertEqual(_select_block(mit), _select_block(ohne))

    def test_the_source_line_carries_the_timestamp(self):
        html = _client().get("/").get_data(as_text=True)
        self.assertIn("Quelle: Nasdaq", html)
        self.assertIn("Stand 08.11.2026 06:00 UTC", html)

    def test_a_stale_calendar_is_labelled_as_the_last_known_state(self):
        kalender = _FakeCalendar({"2026-11-08": [_row("AAA")]}, state="stale")
        html = _client(kalender).get("/").get_data(as_text=True)
        self.assertIn("letzter Stand, Quelle antwortet nicht", html)

    def test_no_configured_source_is_said_plainly(self):
        html = _client(NullEarningsAdapter()).get("/").get_data(as_text=True)
        self.assertIn("Kalender nicht verfügbar", html)
        self.assertIn("Quelle", html)
        self.assertEqual(html.count('class="cal-row"'), 0)


if __name__ == "__main__":
    unittest.main()
