"""What is left of the calendar on ``GET /``: nothing - and it must stay that way.

This file used to own the earnings-calendar block: three trading-day tabs, the
per-day outage labels, the option filter and the default tab. The order removed
that block together with the rest of the old screen, so its 31 page tests
described markup that no longer exists.

The four tests below are the part that still has a subject at HEAD:

* a calendar adapter that raises cannot take the page down - ``index()`` no
  longer asks it at all, so nothing it does can reach the render,
* ``/healthz`` answers without asking either adapter (liveness stays cheap),
* ``?no_calendar`` - in every spelling a bookmark can produce - really means the
  adapter is not asked at all.

The funnel page itself is pinned in ``test_gap_scan_ui.py`` and its demolition in
``test_earnings_scan_ui.py``.
"""

import unittest

import arrow

from paperbroker.server import create_app

# 2026-11-13 12:00 UTC is 07:00 in New York - the US calendar day is still the 13th.
CLOCK = lambda: arrow.get("2026-11-13T12:00:00Z")  # noqa: E731

STAMP = "2026-11-13T06:00:00Z"


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
        self.option_calls = 0

    def get_optionable_symbols(self):
        self.option_calls += 1
        return {"symbols": None, "state": "unavailable",
                "fetched_at_utc": None, "error": None}

    def get_calendar_days(self, days):
        self.calls += 1
        if self.boom:
            raise RuntimeError("calendar adapter exploded")
        geliefert = {
            tag: {"date": tag, "rows": list(self.rows_by_day.get(tag, [])),
                  "state": self.state, "fetched_at_utc": STAMP,
                  "payload_ok": True, "skipped": 0}
            for tag in days.values()
        }
        return {"days": geliefert, "state": self.state, "fetched_at_utc": STAMP}


class _Chain:
    """Two listed expirations; the page never screens them any more."""

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


class TestTheCalendarCannotBreakThePage(unittest.TestCase):
    def test_a_broken_calendar_still_renders_the_funnel(self):
        # index() builds no calendar view any more, so an exploding adapter
        # cannot even be reached - the page still renders the funnel.
        antwort = _client(_FakeCalendar(boom=True)).get("/")
        html = antwort.get_data(as_text=True)
        self.assertEqual(antwort.status_code, 200)
        self.assertIn("Gap-Spread-Trichter", html)
        self.assertIn('id="gap-funnel"', html)
        self.assertNotIn('class="cal-row"', html)

    def test_healthz_never_touches_the_calendar(self):
        kalender = _FakeCalendar({"2026-11-13": [_row("AAPL")]})
        antwort = _client(kalender).get("/healthz")
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(antwort.get_json(), {"status": "ok"})
        self.assertEqual(kalender.calls, 0)

    def test_healthz_never_touches_the_option_list(self):
        kalender = _FakeCalendar({"2026-11-13": [_row("AAPL")]})
        self.assertEqual(_client(kalender).get("/healthz").status_code, 200)
        self.assertEqual(kalender.option_calls, 0)

    def test_no_calendar_acts_by_its_presence_not_its_value(self):
        # The escape hatch is a flag, so every spelling of "parameter present"
        # must mean off. Reading the VALUE as a truthiness test would tie the
        # answer to the spelling: `?no_calendar=` - the bare flag a bookmark
        # produces - is a falsy string and would spend the query the caller
        # just declined.
        for wert in ("0", "false", ""):
            with self.subTest(wert=wert):
                kalender = _FakeCalendar({"2026-11-13": [_row("AAPL")]})
                antwort = _client(kalender).get(f"/?no_calendar={wert}")
                self.assertEqual(antwort.status_code, 200)
                self.assertEqual(kalender.calls, 0)


if __name__ == "__main__":
    unittest.main()
