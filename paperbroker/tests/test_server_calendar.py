"""Offline tests for the earnings calendar block on the start page (plan C1-C6).

``test_server.py`` stays untouched: this file owns the new block only. The
calendar adapter is faked, so no test here needs the network, and every test
runs against the pinned clock.
"""

import re
import unittest

import arrow

from paperbroker.adapters.earnings.NasdaqEarningsAdapter import NullEarningsAdapter
from paperbroker.earnings_day_calendar import DAY_NEXT, DAY_PREV, DAY_TODAY
from paperbroker.server import create_app

# 2026-11-13 12:00 UTC is 07:00 in New York - the US calendar day is still the 13th.
# A Friday on purpose: it is the day the tabs used to promise a Saturday, and the
# trading-day tabs around it are Thursday / Friday / Monday.
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
    """A calendar adapter that answers from memory and counts its calls.

    ``state_by_day`` overrides ``state`` for single days (an outage is rarely
    all three days at once), and ``omit`` leaves days out of the answer
    entirely - the shape a caller has to read as "not answered" rather than
    "answered empty". The aggregate state is the worst of the delivered days,
    exactly like the real adapter's rule.
    """

    _RANG = {"live": 0, "stale": 1, "unavailable": 2}

    def __init__(self, rows_by_day=None, state="live", boom=False, state_by_day=None, omit=(),
                 option_symbols=None, option_state="unavailable", option_boom=False,
                 error_by_day=None):
        self.rows_by_day = rows_by_day or {}
        self.state = state
        self.boom = boom
        self.state_by_day = dict(state_by_day or {})
        self.omit = tuple(omit)
        # The upstream failure per day, as the real adapter records it: a 403 is
        # an ANSWER and has to reach the page, not just the log.
        self.error_by_day = dict(error_by_day or {})
        # Slice 10: the optionable list is a SECOND answer of the same adapter.
        # Default "unavailable" keeps the old tests unfiltered on purpose.
        self.option_symbols = option_symbols
        self.option_state = option_state
        self.option_boom = option_boom
        self.option_calls = 0
        self.calls = 0
        self.last_days = None

    def get_optionable_symbols(self):
        self.option_calls += 1
        if self.option_boom:
            raise RuntimeError("option list exploded")
        stempel = STAMP if self.option_state in ("live", "cached", "stale") else None
        return {"symbols": self.option_symbols, "state": self.option_state,
                "fetched_at_utc": stempel, "error": None}

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
            eintrag = {
                "date": tag,
                "rows": list(self.rows_by_day.get(tag, [])),
                "state": zustand,
                "fetched_at_utc": STAMP if zustand in ("live", "stale") else None,
                "payload_ok": zustand in ("live", "stale"),
                "skipped": 0,
            }
            if tag in self.error_by_day and zustand == "unavailable":
                eintrag["error"] = dict(self.error_by_day[tag])
                eintrag["live_query_failed"] = True
            geliefert[tag] = eintrag
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
            "2026-11-12": [_row("AAA")],
            "2026-11-13": [_row("BBB")],
            "2026-11-16": [_row("CCC")],
        })
        antwort = _client(kalender).get("/")
        html = antwort.get_data(as_text=True)
        self.assertEqual(antwort.status_code, 200)
        self.assertIn("Earnings-Kalender", html)
        self.assertEqual(html.count('class="cal-group"'), 3)
        for tag in ("12.11.2026", "13.11.2026", "16.11.2026"):
            self.assertIn(tag, html)
        # The words are used where they are literally true; the third tab is a
        # Monday, so it names itself instead of promising "morgen".
        for label in ("gestern · Do 12.11.", "heute · Fr 13.11.", "Mo 16.11."):
            self.assertIn(label, html)

    def test_a_row_links_to_the_start_page_with_the_ticker(self):
        # C2
        kalender = _FakeCalendar({"2026-11-13": [_row("AAPL")]})
        html = _client(kalender).get("/").get_data(as_text=True)
        self.assertIn('href="/?ticker=AAPL#screener-form"', html)
        self.assertIn('id="screener-form"', html)
        self.assertNotIn("/ticker/AAPL", html)

    def test_only_the_top_25_are_linked_and_the_total_is_named(self):
        # C3
        rows = [_row(f"{chr(65 + i // 26)}{chr(65 + i % 26)}", cap=(40 - i) * 1000) for i in range(40)]
        kalender = _FakeCalendar({"2026-11-13": rows})
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
        kalender = _FakeCalendar({"2026-11-13": [_row("AAPL")]})
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
        self.assertIn("Stand 13.11.2026 06:00 UTC", html)

    def test_a_stale_calendar_is_labelled_as_the_last_known_state(self):
        kalender = _FakeCalendar({"2026-11-13": [_row("AAA")]}, state="stale")
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
                kalender = _FakeCalendar({"2026-11-13": [_row("AAPL")]})
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
            {"2026-11-12": [_row("AAA")], "2026-11-16": rows},
            state_by_day={"2026-11-13": "unavailable"},
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

        montag = _group(html, "Mo 16.11.")
        self.assertIn("25 von 241", montag)
        self.assertNotIn("Tag nicht abrufbar", montag)
        self.assertNotIn("keine Einträge", montag)

        # The header names the single outage and does not claim a stale answer.
        self.assertIn("ein Tag konnte nicht abgerufen werden", html)
        self.assertNotIn("letzter Stand, Quelle antwortet nicht", html)
        self.assertLess(html.index("ein Tag konnte nicht abgerufen werden"), html.index('class="cal-groups"'))

    def test_two_answered_empty_days_carry_no_warning(self):
        # Regression against a standing warning: an empty answered day IS the
        # answer, and only "keine Einträge" may be shown for it.
        rows = [_row(f"SY{i:02d}", cap=241 - i) for i in range(241)]
        kalender = _FakeCalendar({"2026-11-16": rows})
        html = _client(kalender).get("/").get_data(as_text=True)

        self.assertEqual(html.count("keine Einträge"), 2)
        self.assertNotIn("Tag nicht abrufbar", html)
        self.assertNotIn("konnte nicht abgerufen werden", html)
        self.assertNotIn("antwortet nicht", html)
        self.assertIn("25 von 241", html)
        self.assertIn("Quelle: Nasdaq · Stand 13.11.2026 06:00 UTC</p>", html)

    def test_a_stale_day_shows_its_rows_and_is_named_as_the_last_state(self):
        kalender = _FakeCalendar(
            {"2026-11-12": [_row("AAA")], "2026-11-13": [_row("BBB")], "2026-11-16": [_row("CCC")]},
            state_by_day={"2026-11-12": "stale"},
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
            {"2026-11-16": [_row("CCC")]},
            state_by_day={"2026-11-12": "stale", "2026-11-13": "unavailable"},
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

    def test_a_blocked_source_names_the_reason_instead_of_going_silent(self):
        # Measured symptom: the start page said only "Quelle antwortet nicht"
        # while the source had answered 403 - an event, not a silence.
        block = {"code": "source_blocked", "http_status": 403,
                 "message": "the source blocked this client with HTTP 403"}
        kalender = _FakeCalendar({}, state="unavailable",
                                 error_by_day={"2026-11-12": block, "2026-11-13": block,
                                               "2026-11-16": block})
        html = _client(kalender).get("/").get_data(as_text=True)

        self.assertIn("Quelle antwortet nicht", html)
        self.assertIn("Grund: Quelle blockt die Anfrage (HTTP 403)", html)
        self.assertEqual(
            html.count("Tag nicht abrufbar · Quelle blockt die Anfrage (HTTP 403)"), 3)
        # Nothing to scan stays nothing to scan: the button keeps its disabled
        # state and its own title.
        self.assertEqual(html.count('data-scan-day="2026-11-13" disabled'), 1)

    def test_one_blocked_day_names_its_reason_once_next_to_the_partial_sentence(self):
        block = {"code": "source_blocked", "http_status": 403,
                 "message": "the source blocked this client with HTTP 403"}
        kalender = _FakeCalendar({"2026-11-12": [_row("AAA")], "2026-11-16": [_row("CCC")]},
                                 state="live", state_by_day={"2026-11-13": "unavailable"},
                                 error_by_day={"2026-11-13": block})
        html = _client(kalender).get("/").get_data(as_text=True)

        self.assertIn("ein Tag konnte nicht abgerufen werden", html)
        self.assertIn("Grund: Quelle blockt die Anfrage (HTTP 403)", html)
        # Header sentence plus the one day's own line - never once per day twice.
        self.assertEqual(html.count("Quelle blockt die Anfrage"), 2)

    def test_a_day_the_answer_omits_is_an_outage_not_an_empty_day(self):
        # The adapter's answer simply has no entry for yesterday: absence of an
        # answer is no answer - it may not be rendered as a day without rows.
        kalender = _FakeCalendar({"2026-11-13": [_row("BBB")], "2026-11-16": [_row("CCC")]},
                                 omit=("2026-11-12",))
        html = _client(kalender).get("/").get_data(as_text=True)

        self.assertIn("Tag nicht abrufbar", _group(html, "gestern"))
        self.assertIn("ein Tag konnte nicht abgerufen werden", html)


def _filter_note(html):
    """The calendar's option-filter sentence, isolated from the rest of the page."""
    treffer = re.search(r'<p class="note cal-filter">(.*?)</p>', html, re.S)
    assert treffer is not None, "option filter note missing"
    return treffer.group(1)


class TestCalendarTabs(unittest.TestCase):
    """Plan E: day tabs with a counter, ONE visible body, a real table head."""

    def _html(self, **kwargs):
        rows = {
            "2026-11-12": [_row("AAA", cap=30)],
            "2026-11-13": [_row("BBB", cap=20), _row("CCC", cap=10)],
            "2026-11-16": [_row("DDD", cap=5)],
        }
        rows.update(kwargs.pop("rows", {}))
        option_symbols = kwargs.pop("option_symbols", ["AAA", "BBB", "CCC", "DDD"])
        kalender = _FakeCalendar(rows, option_symbols=option_symbols,
                                 option_state=kwargs.pop("option_state", "live"))
        return _client(kalender).get("/").get_data(as_text=True)

    def test_three_tabs_carry_their_day_and_counter(self):
        # E1
        html = self._html()
        self.assertEqual(len(re.findall(r'<button[^>]*class="cal-tab[^"]*"[^>]*data-day=', html)), 3)
        for tag in ("2026-11-12", "2026-11-13", "2026-11-16"):
            self.assertIn(f'data-day="{tag}"', html)
        # Label + weekday/date + the number of optionable titles of that day.
        self.assertRegex(html, r'data-day="2026-11-12"[^>]*>[^<]*gestern[^<]*· 1<')
        self.assertRegex(html, r'data-day="2026-11-13"[^>]*>[^<]*heute[^<]*· 2<')
        self.assertRegex(html, r'data-day="2026-11-16"[^>]*>[^<]*Mo 16\.11\.[^<]*· 1<')

    def test_a_friday_offers_thursday_and_monday_never_the_weekend(self):
        """Measured: on a Friday the third tab read "morgen · Sa 14.11. · 0".

        The three tabs are trading days. Asked for a Friday the page must ask
        the source for Thursday / Friday / Monday - the weekend never reaches
        the source and never reaches the page.
        """
        kalender = _FakeCalendar({})
        html = _client(kalender).get("/").get_data(as_text=True)

        self.assertEqual(kalender.last_days, {
            DAY_PREV: "2026-11-12",     # Donnerstag
            DAY_TODAY: "2026-11-13",    # Freitag
            DAY_NEXT: "2026-11-16",     # Montag
        })
        for tag in ("2026-11-12", "2026-11-13", "2026-11-16"):
            self.assertIn(f'data-day="{tag}"', html)
        for wochenende in ("2026-11-14", "2026-11-15", "Sa 14.11.", "So 15.11."):
            self.assertNotIn(wochenende, html)
        # The label says "gestern" for the Thursday (it really is yesterday)
        # and names the Monday by date - it is not tomorrow.
        self.assertIn("gestern · Do 12.11.", html)
        self.assertIn("Mo 16.11.", html)
        self.assertNotIn("morgen ·", html)

    def test_exactly_one_day_body_is_visible_and_it_is_today(self):
        # E2
        html = self._html()
        self.assertEqual(html.count('class="cal-body show"'), 1)
        self.assertEqual(html.count('class="cal-body"'), 2)   # the two hidden ones
        self.assertRegex(html, r'<div class="cal-body show"[^>]*data-day="2026-11-13"')

    def test_the_table_has_a_real_column_head(self):
        # E3
        html = self._html()
        self.assertRegex(
            html,
            r"<thead><tr><th>Zeit</th><th>Ticker</th><th>Unternehmen</th>"
            r"<th>EPS erwartet \(USD\)</th><th>Marktkap\. \(Mrd USD\)</th></tr></thead>",
        )
        self.assertRegex(html, r"<td class=\"num\">[\d.,]+</td>")   # market cap, right aligned
        self.assertIn('class="cal-scroll"', html)
        self.assertIn("max-height:264px", html)

    def test_the_collapse_switch_exists_and_the_rule_hides_tabs_and_table(self):
        # E4: the actual class flip is JS; the structure and the CSS contract
        # are what a server-side test can pin down.
        html = self._html()
        self.assertIn('data-cal-toggle', html)
        self.assertIn(">ausblenden<", html)
        self.assertIn(".cal.collapsed .cal-groups", html)

    def test_a_symbol_click_can_fill_the_ticker_field(self):
        # E5: the link keeps its href (no-JS fallback) and carries the symbol
        # for the small page script.
        html = self._html()
        self.assertIn('data-symbol="BBB"', html)
        self.assertIn('href="/?ticker=BBB#screener-form"', html)


class TestCalendarOptionFilter(unittest.TestCase):
    """Plan E: the filter note names its number - or says "ungefiltert"."""

    def test_hidden_titles_are_counted_and_not_linked(self):
        # E6
        kalender = _FakeCalendar(
            {"2026-11-13": [_row("AAPL", cap=2000), _row("IX", cap=5000)]},
            option_symbols=["AAPL"],
            option_state="live",
        )
        html = _client(kalender).get("/").get_data(as_text=True)
        self.assertIn("1 Titel ohne gelistete Optionen ausgeblendet", _filter_note(html))
        self.assertNotIn('data-symbol="IX"', html)
        self.assertNotIn('href="/?ticker=IX#screener-form"', html)
        # The tab counter names the optionable total, not the raw day list.
        self.assertRegex(html, r'data-day="2026-11-13"[^>]*>[^<]*· 1<')

    def test_an_unavailable_list_says_unfiltered_and_names_no_number(self):
        # E7
        kalender = _FakeCalendar({"2026-11-13": [_row("AAPL"), _row("IX")]})
        html = _client(kalender).get("/").get_data(as_text=True)
        note = _filter_note(html)
        self.assertIn("ungefiltert", note)
        self.assertNotRegex(note, r"\d")
        # Unfiltered means unfiltered: the title without options stays.
        self.assertIn('data-symbol="IX"', html)

    def test_a_broken_option_list_does_not_break_the_page(self):
        # E8
        kalender = _FakeCalendar({"2026-11-13": [_row("AAPL")]}, option_boom=True)
        antwort = _client(kalender).get("/")
        html = antwort.get_data(as_text=True)
        self.assertEqual(antwort.status_code, 200)
        self.assertIn("ungefiltert", _filter_note(html))
        self.assertIn('data-symbol="AAPL"', html)
        self.assertIn("Instrument", html)

    def test_a_stale_list_filters_and_says_it_is_the_last_state(self):
        # E9
        kalender = _FakeCalendar(
            {"2026-11-13": [_row("AAPL"), _row("IX")]},
            option_symbols=["AAPL"],
            option_state="stale",
        )
        html = _client(kalender).get("/").get_data(as_text=True)
        self.assertIn("Options-Liste aus letztem Stand", html)
        self.assertIn("Options-Liste: Stand 13.11.2026 06:00 UTC", html)
        self.assertNotIn('data-symbol="IX"', html)

    def test_healthz_never_touches_the_option_list(self):
        # E10
        kalender = _FakeCalendar({"2026-11-13": [_row("AAPL")]})
        client = _client(kalender)
        self.assertEqual(client.get("/healthz").status_code, 200)
        self.assertEqual(kalender.option_calls, 0)


class TestCalendarDefaultDay(unittest.TestCase):
    """The tab the page opens on must be the first day that has content.

    A hard-wired "today" opens an empty tab while the next day lists
    everything - the block's only content would sit behind a tab the
    reader never clicks. These tests pin the rule on the rendered markup,
    because that is what a reader sees: exactly one ``is-active`` tab and
    exactly one visible body, both on the same day.
    """

    def _html(self, kalender):
        antwort = _client(kalender).get("/")
        assert antwort.status_code == 200, "the start page must not fail"
        return antwort.get_data(as_text=True)

    @staticmethod
    def _active_days(html):
        """Every day whose tab is marked active (one is expected)."""
        return re.findall(r'<button[^>]*class="cal-tab is-active"[^>]*data-day="([^"]+)"', html)

    @staticmethod
    def _visible_days(html):
        """Every day whose body is visible (one is expected)."""
        return re.findall(r'<div class="cal-body show"[^>]*data-day="([^"]+)"', html)

    def _assert_opens_on(self, html, tag):
        self.assertEqual(self._active_days(html), [tag])
        self.assertEqual(self._visible_days(html), [tag])
        self.assertEqual(html.count('class="cal-body show"'), 1)
        self.assertEqual(html.count('class="cal-body"'), 2)   # the two hidden ones
        # aria-selected must agree with the visual state, or a screen reader
        # announces a different day than the one the page shows.
        self.assertRegex(html, rf'<button[^>]*class="cal-tab is-active"[^>]*data-day="{tag}"[^>]*aria-selected="true"')

    def test_an_empty_today_yields_to_the_next_day_with_rows(self):
        # The measured case: 0 titles today, 191 on the next trading day.
        rows = [_row(f"SY{i:02d}", cap=1000 - i) for i in range(191)]
        kalender = _FakeCalendar({"2026-11-16": rows})
        html = self._html(kalender)

        self._assert_opens_on(html, "2026-11-16")
        # The content is in the opened body, not merely reachable.
        self.assertIn('href="/?ticker=SY00#screener-form"', html)
        self.assertIn("keine Einträge", _group(html, "heute"))
        self.assertIn("25 von 191", _group(html, "Mo 16.11."))

    def test_a_today_with_rows_stays_the_default(self):
        # Regression: the rule must not prefer an earlier day that has rows.
        kalender = _FakeCalendar({"2026-11-12": [_row("AAA")],
                                  "2026-11-13": [_row("BBB")],
                                  "2026-11-16": [_row("CCC")]})
        self._assert_opens_on(self._html(kalender), "2026-11-13")

    def test_an_empty_prev_day_yields_to_today(self):
        # Order check in the other direction: prev is only the fallback when
        # today is empty, never the default merely because it has rows.
        kalender = _FakeCalendar({"2026-11-12": [_row("AAA")],
                                  "2026-11-13": [_row("BBB")]})
        self._assert_opens_on(self._html(kalender), "2026-11-13")

    def test_three_empty_days_keep_today_as_the_default(self):
        # No content anywhere: the page still opens on "heute", it does not
        # raise and it does not leave every body hidden.
        kalender = _FakeCalendar({})
        html = self._html(kalender)
        self._assert_opens_on(html, "2026-11-13")
        self.assertEqual(html.count("keine Einträge"), 3)

    def test_a_failed_today_never_becomes_the_default(self):
        # "unavailable" is no answer, not an empty day: tomorrow's rows must
        # win, and the failed tab must not look active.
        kalender = _FakeCalendar(
            {"2026-11-16": [_row("CCC")]},
            state_by_day={"2026-11-13": "unavailable"},
        )
        html = self._html(kalender)
        self._assert_opens_on(html, "2026-11-16")
        self.assertIn("Tag nicht abrufbar", _group(html, "heute"))


if __name__ == "__main__":
    unittest.main()
