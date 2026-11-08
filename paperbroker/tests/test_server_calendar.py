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
    """A calendar adapter that answers from memory and counts its calls.

    ``state_by_day`` overrides ``state`` for single days (an outage is rarely
    all three days at once), and ``omit`` leaves days out of the answer
    entirely - the shape a caller has to read as "not answered" rather than
    "answered empty". The aggregate state is the worst of the delivered days,
    exactly like the real adapter's rule.
    """

    _RANG = {"live": 0, "stale": 1, "unavailable": 2}

    def __init__(self, rows_by_day=None, state="live", boom=False, state_by_day=None, omit=()):
        self.rows_by_day = rows_by_day or {}
        self.state = state
        self.boom = boom
        self.state_by_day = dict(state_by_day or {})
        self.omit = tuple(omit)
        self.calls = 0
        self.last_days = None

    def get_calendar_days(self, days):
        self.calls += 1
        self.last_days = dict(days)
        if self.boom:
            raise RuntimeError("calendar adapter exploded")
        geliefert = {}
        for _label, tag in days.items():
            if tag in self.omit:
                continue
            zustand = self.state_by_day.get(tag, self.state)
            geliefert[tag] = {
                "date": tag,
                "rows": list(self.rows_by_day.get(tag, [])),
                "state": zustand,
                "fetched_at_utc": STAMP if zustand in ("live", "stale") else None,
                "payload_ok": zustand in ("live", "stale"),
                "skipped": 0,
            }
        gesamt = "unavailable"
        if geliefert:
            gesamt = max((e["state"] for e in geliefert.values()),
                         key=lambda z: self._RANG.get(z, 2))
        # Like the adapter: the aggregate stamp is the oldest delivered one, and
        # no delivered day means no stamp at all (never a fresh-looking "now").
        stempel = [e["fetched_at_utc"] for e in geliefert.values() if e["fetched_at_utc"]]
        return {"days": geliefert, "state": gesamt, "fetched_at_utc": min(stempel) if stempel else None}


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


def _group(html, label):
    """One rendered calendar group: from its heading up to the next boundary.

    The slice ends at the next group's container marker or at the section end,
    so a nested ``<ul>`` cannot make the match swallow the neighbouring group
    and the last group cannot swallow the rest of the page.
    """
    kopf = html.index(f"<h3>{label}")
    grenzen = [grenze for grenze in (html.find('<div class="cal-group">', kopf + 1),
                                     html.find("</section>", kopf + 1)) if grenze != -1]
    assert grenzen, f"group {label!r} has no closing boundary"
    return html[kopf:min(grenzen)]


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

    def test_no_calendar_acts_by_its_presence_not_its_value(self):
        # The escape hatch is a flag, so every spelling of "parameter present"
        # must mean off. Reading the VALUE as a truthiness test ties the answer
        # to the spelling: `?no_calendar=` - the bare flag a bookmark produces -
        # is a falsy string and shows the calendar the caller switched off.
        # The marker is the block's own markup, not the text "Earnings-Kalender":
        # that string also sits in the stylesheet comment and would make the
        # assertion pass for the wrong reason.
        for wert in ("0", "false", ""):
            with self.subTest(wert=wert):
                kalender = _FakeCalendar({"2026-11-08": [_row("AAPL")]})
                html = _client(kalender).get(f"/?no_calendar={wert}").get_data(as_text=True)
                self.assertNotIn('id="cal-title"', html)
                self.assertNotIn('class="cal"', html)
                self.assertEqual(html.count('class="cal-row"'), 0)
                self.assertEqual(kalender.calls, 0)


class TestCalendarDayOutages(unittest.TestCase):
    """A day nobody could ask about must not read like a day without earnings.

    The three groups are derived from the adapter's per-day ``state``, so every
    test here builds the answer per day - a uniform fake would hide exactly the
    bug this class covers.
    """

    def test_one_failed_day_is_labelled_and_the_other_two_stay_as_they_were(self):
        rows = [_row(f"SY{i:02d}", cap=241 - i) for i in range(241)]
        kalender = _FakeCalendar(
            {"2026-11-07": [_row("AAA")], "2026-11-09": rows},
            state_by_day={"2026-11-08": "unavailable"},
        )
        html = _client(kalender).get("/").get_data(as_text=True)

        heute = _group(html, "heute")
        self.assertIn("Tag nicht abrufbar", heute)
        self.assertNotIn("keine Einträge", heute)
        # No invented row for the failed day: the group holds no link and no list.
        self.assertNotIn("href=", heute)
        self.assertNotIn('class="cal-row"', heute)
        self.assertNotIn(" von ", heute)

        gestern = _group(html, "gestern")
        self.assertIn('href="/?ticker=AAA#screener-form"', gestern)
        self.assertIn("1 von 1", gestern)
        self.assertNotIn("Tag nicht abrufbar", gestern)

        morgen = _group(html, "morgen")
        self.assertIn("25 von 241", morgen)
        self.assertNotIn("Tag nicht abrufbar", morgen)
        self.assertNotIn("keine Einträge", morgen)

        # The header names the single outage and does not claim a stale answer.
        self.assertIn("ein Tag konnte nicht abgerufen werden", html)
        self.assertNotIn("letzter Stand, Quelle antwortet nicht", html)
        self.assertLess(html.index("ein Tag konnte nicht abgerufen werden"), html.index('class="cal-groups"'))

    def test_two_answered_empty_weekend_days_carry_no_warning(self):
        # Regression against a standing warning: on a Saturday/Sunday the two
        # empty days ARE the answer, and only "keine Einträge" may be shown.
        rows = [_row(f"SY{i:02d}", cap=241 - i) for i in range(241)]
        kalender = _FakeCalendar({"2026-11-09": rows})
        html = _client(kalender).get("/").get_data(as_text=True)

        self.assertEqual(html.count("keine Einträge"), 2)
        self.assertNotIn("Tag nicht abrufbar", html)
        self.assertNotIn("konnte nicht abgerufen werden", html)
        self.assertNotIn("antwortet nicht", html)
        self.assertIn("25 von 241", html)
        self.assertIn("Quelle: Nasdaq · Stand 08.11.2026 06:00 UTC</p>", html)

    def test_a_stale_day_shows_its_rows_and_is_named_as_the_last_state(self):
        kalender = _FakeCalendar(
            {"2026-11-07": [_row("AAA")], "2026-11-08": [_row("BBB")], "2026-11-09": [_row("CCC")]},
            state_by_day={"2026-11-07": "stale"},
        )
        html = _client(kalender).get("/").get_data(as_text=True)

        self.assertIn("letzter Stand, Quelle antwortet nicht", html)
        self.assertNotIn("konnte nicht abgerufen werden", html)
        gestern = _group(html, "gestern")
        self.assertIn('href="/?ticker=AAA#screener-form"', gestern)
        self.assertIn("1 von 1", gestern)
        self.assertNotIn("Tag nicht abrufbar", html)
        self.assertNotIn("keine Einträge", html)

    def test_a_failed_and_a_stale_day_are_named_in_one_header(self):
        kalender = _FakeCalendar(
            {"2026-11-09": [_row("CCC")]},
            state_by_day={"2026-11-07": "stale", "2026-11-08": "unavailable"},
        )
        html = _client(kalender).get("/").get_data(as_text=True)

        self.assertIn("ein Tag konnte nicht abgerufen werden", html)
        self.assertIn("letzter Stand, Quelle antwortet nicht", html)
        self.assertIn("Tag nicht abrufbar", _group(html, "heute"))
        self.assertIn("keine Einträge", _group(html, "gestern"))

    def test_all_three_failed_days_keep_three_labelled_groups(self):
        kalender = _FakeCalendar({}, state="unavailable")
        html = _client(kalender).get("/").get_data(as_text=True)

        self.assertIn("Quelle antwortet nicht", html)
        self.assertEqual(html.count("Tag nicht abrufbar"), 3)
        self.assertEqual(html.count('class="cal-group"'), 3)
        self.assertEqual(html.count("keine Einträge"), 0)
        self.assertEqual(html.count('class="cal-row"'), 0)
        self.assertNotIn("Kalender nicht verfügbar", html)
        # No day was answered, so no timestamp may suggest a fresh answer: the
        # source line runs straight from the source name into the outage note.
        self.assertIn("Quelle: Nasdaq · Quelle antwortet nicht</p>", html)

    def test_a_day_the_answer_omits_is_an_outage_not_an_empty_day(self):
        # The adapter's answer simply has no entry for yesterday: absence of an
        # answer is no answer - it may not be rendered as a day without rows.
        kalender = _FakeCalendar({"2026-11-08": [_row("BBB")], "2026-11-09": [_row("CCC")]},
                                 omit=("2026-11-07",))
        html = _client(kalender).get("/").get_data(as_text=True)

        self.assertIn("Tag nicht abrufbar", _group(html, "gestern"))
        self.assertIn("ein Tag konnte nicht abgerufen werden", html)


if __name__ == "__main__":
    unittest.main()
