"""Offline tests for the closing-gap scan as an endpoint (GET /api/v1/gap-scan).

``test_gap_scan.py`` owns the measurement and the selection; this file owns the
HTTP contract around them. Both sources are faked - the calendar from a dict of
rows, the history from a dict of bars - so no test here touches the network, and
every test runs against the pinned clock.

What is pinned, in the order of the requirement:

* the answer names its mode and the day it measured,
* ``counts`` has exactly the six agreed fields and describes the WHOLE day even
  when ``max_candidates`` shows only the best few (the gap stage has no cap),
* a pre-market row of the reaction day is certain, an after-market row has no
  gap yet and is not even asked,
* a row of the previous day whose session the source deleted is uncertain,
* a day that is no date is a 400 that never reaches the calendar,
* a day that is still OPEN in New York is a 400 with a message, not a quiet
  200 - and it costs no source call either; the same day after 16:00 ET is
  scanned, because that session has a close,
* the previous day is the previous TRADING day: a Monday is measured against
  its Friday, a day after Thanksgiving against the Wednesday before it,
* a calendar that cannot answer is a 503 - with the upstream code and status
  when it answered 403 instead of staying silent,
* a day where every symbol fails is still a 200 with ``selected: 0``,
* two requests for the same day read the history ONCE (shared adapter + cache).
"""

import json
import unittest
from datetime import date, timedelta
from unittest import mock

import arrow

from paperbroker.earnings_day_calendar import DAY_PREV
from paperbroker.gap_scan import DEFAULT_LIMIT, GapBar, GapSourceError
from paperbroker.server import create_app

# 2026-11-12 21:00 UTC is 16:00 in New York - the US day is still the 12th, so
# the measured reaction day (11th) is one CLOSED session behind the clock. The
# endpoint refuses a day that has not closed, so the fixture day must lie in the
# past: a pinned clock is a fact of this file, not a convenience.
CLOCK = lambda: arrow.get("2026-11-12T21:00:00Z")  # noqa: E731
# 16:00 ET is the close itself: from this hour on the day HAS a close.
# 21:30Z is 16:30 ET (22:30 CET) - the evening scan of the day that just closed.
AFTER_CLOSE_CLOCK = lambda: arrow.get("2026-11-12T21:30:00Z")  # noqa: E731
# 18:00Z is 13:00 ET (19:00 CET) - the same New York day, still open.
BEFORE_CLOSE_CLOCK = lambda: arrow.get("2026-11-12T18:00:00Z")  # noqa: E731
# A clock the day AFTER the two edge cases below, so both are closed days:
# 2026-11-09 is a Monday, 2026-11-27 the session after Thanksgiving.
MONDAY_CLOCK = lambda: arrow.get("2026-11-10T21:00:00Z")   # noqa: E731
HOLIDAY_CLOCK = lambda: arrow.get("2026-11-30T21:00:00Z")  # noqa: E731

DAY = "2026-11-11"
PREVIOUS = "2026-11-10"
REACTION = date(2026, 11, 11)
# The session before the fixture reaction day - what ``bars`` builds.
BEFORE = date(2026, 11, 10)


def calendar_row(symbol, session=None, name=None):
    """One calendar row in the adapter's shape: keys, not classes."""
    return {
        "symbol": symbol,
        "name": name if name is not None else f"{symbol} Inc.",
        "session": session,
        "eps_forecast": "$1.10",
        "market_cap": 1000,
        "fiscal_quarter_ending": "Sep/2026",
    }


class _FakeCalendar:
    """A calendar adapter that answers from memory and counts its calls.

    ``state_by_day`` overrides ``state`` for single days, ``omit`` leaves days
    out of the answer entirely (the shape a caller has to read as "not
    answered"), and ``error_by_day`` records what the source DID when it
    blocked a day - a 403 is an answer, not silence.
    """

    _RANG = {"live": 0, "stale": 1, "unavailable": 2}

    def __init__(self, rows_by_day=None, state="live", state_by_day=None, boom=False,
                 omit=(), error_by_day=None):
        self.rows_by_day = rows_by_day or {}
        self.state = state
        self.state_by_day = dict(state_by_day or {})
        self.boom = boom
        self.omit = tuple(omit)
        self.error_by_day = dict(error_by_day or {})
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
            eintrag = {
                "date": tag,
                "rows": list(self.rows_by_day.get(tag, [])),
                "state": zustand,
                "fetched_at_utc": "2026-11-11T20:00:00Z" if zustand in ("live", "stale") else None,
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
        return {"days": geliefert, "state": gesamt, "fetched_at_utc": None}


class _History:
    """A history adapter with ``fetch_daily``'s signature - one answer per symbol.

    ``errors[symbol]`` is a ``GapSourceError`` raised INSTEAD of the bars, so
    one test can break one title (or all of them) without a transport.
    """

    def __init__(self, bars=None, errors=None):
        self.bars = bars or {}
        self.errors = errors or {}
        self.calls = []

    def fetch_daily(self, symbol, fromdate, limit=DEFAULT_LIMIT):
        self.calls.append(symbol)
        error = self.errors.get(symbol)
        if error is not None:
            raise error
        return list(self.bars.get(symbol, []))


def bars(close, previous_close=10.0, volume=1_000_000.0, reaction=REACTION):
    """Two sessions: the reaction day and the session before it."""
    return [
        GapBar(session_date=reaction, open=close, close=close, volume=volume),
        GapBar(session_date=reaction - timedelta(days=1), open=previous_close,
               close=previous_close, volume=volume),
    ]


def _history():
    """Three measurable titles: AAA +10 %, BBB +4 %, CCC -10 %."""
    return _History(bars={
        # AAA comes from the PREVIOUS day: its session is gone (unknown).
        "AAA": bars(11.0, volume=2_000_000.0),
        "BBB": bars(10.4),
        "CCC": bars(9.0, volume=1_500_000.0),
    })


def _calendar(state="live", **kwargs):
    return _FakeCalendar(
        rows_by_day={
            PREVIOUS: [
                calendar_row("AAA", session=None),          # session deleted
                calendar_row("BBB", session="before_market"),
            ],
            DAY: [
                calendar_row("CCC", session="before_market", name="Cee Corporation"),
                calendar_row("DDD", session="after_market"),  # no gap yet
            ],
        },
        state=state,
        **kwargs,
    )


def _client(calendar=None, history=None, now_fn=CLOCK):
    return create_app(
        quote_adapter=object(),
        earnings_adapter=calendar or _calendar(),
        gap_adapter=history or _history(),
        now_fn=now_fn,
    ).test_client()


def _scan(query=f"?day={DAY}", calendar=None, history=None):
    """One gap-scan request, plus the two fakes so a test can inspect them."""
    kalender = calendar or _calendar()
    quelle = history or _history()
    antwort = _client(kalender, quelle).get(f"/api/v1/gap-scan{query}")
    return antwort, antwort.get_json(), kalender, quelle


def _by_symbol(candidates):
    return {eintrag["symbol"]: eintrag for eintrag in candidates}


class _CountingTransport:
    """The transport of the REAL adapter, for the tests about the real adapter.

    ``create_app`` builds its own shared adapter when no ``gap_adapter`` is
    injected, so its cache cannot be proven with a fake adapter - but no test
    may touch the network either. This transport counts the requests and answers
    one fixed Nasdaq payload: the counter IS the assertion.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append(url)
        rows = [
            {"date": "11/11/2026", "open": "$11.00", "high": "$11.00", "low": "$11.00",
             "close": "$11.00", "volume": "2,000,000"},
            {"date": "11/10/2026", "open": "$10.00", "high": "$10.00", "low": "$10.00",
             "close": "$10.00", "volume": "2,000,000"},
        ]
        return 200, json.dumps({"data": {"tradesTable": {"rows": rows}}}).encode("utf-8")


class GapScanEndpointTest(unittest.TestCase):
    def test_the_scan_names_its_mode_and_the_day(self):
        antwort, koerper, kalender, _quelle = _scan()

        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(koerper["mode"], "post_earnings_gap")
        self.assertEqual(koerper["day"], DAY)
        self.assertEqual(koerper["day_state"], "live")
        self.assertEqual(koerper["threshold_pct"], 2.0)
        self.assertEqual(koerper["min_turnover_usd"], 5_000_000.0)
        self.assertEqual(koerper["max_candidates"], 30)
        # Both calendar days were asked for, in one call: the reaction day and
        # the calendar day before it.
        self.assertEqual(kalender.calls, 1)
        self.assertEqual(sorted((kalender.last_days or {}).values()), [PREVIOUS, DAY])

    def test_the_counts_describe_what_was_checked(self):
        antwort, koerper, _kalender, _quelle = _scan()

        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(
            sorted(koerper["counts"]),
            sorted(["candidate_rows", "gaps_computed", "over_threshold", "turnover_passed",
                    "selected", "failures"]),
        )
        self.assertEqual(koerper["counts"], {
            "candidate_rows": 3,   # AAA, BBB, CCC - DDD is after the close
            "gaps_computed": 3,
            "over_threshold": 3,
            "turnover_passed": 3,
            "selected": 3,
            "failures": 0,
        })
        self.assertEqual([e["symbol"] for e in koerper["candidates"]], ["AAA", "CCC", "BBB"])

    def test_the_cap_limits_the_answer_not_the_measurement(self):
        # The whole point of "no cap on the gap stage": asking for two entries
        # must still measure all three titles.
        antwort, koerper, _kalender, quelle = _scan(f"?day={DAY}&max_candidates=2")

        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(koerper["counts"]["candidate_rows"], 3)
        self.assertEqual(koerper["counts"]["gaps_computed"], 3)
        self.assertEqual(koerper["counts"]["selected"], 2)
        self.assertEqual(len(koerper["candidates"]), 2)
        self.assertEqual(sorted(quelle.calls), ["AAA", "BBB", "CCC"])

    def test_a_pre_market_row_of_the_day_is_certain(self):
        _antwort, koerper, _kalender, quelle = _scan()

        ccc = _by_symbol(koerper["candidates"])["CCC"]
        self.assertFalse(ccc["session_uncertain"])
        self.assertEqual(ccc["session"], "before_market")
        self.assertEqual(ccc["earnings_date"], DAY)
        self.assertEqual(ccc["name"], "Cee Corporation")
        self.assertEqual(ccc["direction"], "down")
        self.assertEqual(ccc["strategy"], "bear_put_debit")
        # The after-market row of the same day has no gap YET - it is not asked.
        self.assertNotIn("DDD", quelle.calls)

    def test_a_row_of_the_previous_day_without_a_session_is_marked_uncertain(self):
        _antwort, koerper, _kalender, _quelle = _scan()
        kandidaten = _by_symbol(koerper["candidates"])

        aaa = kandidaten["AAA"]
        self.assertTrue(aaa["session_uncertain"])
        self.assertEqual(aaa["session"], "unknown")
        self.assertEqual(aaa["earnings_date"], PREVIOUS)
        self.assertEqual(aaa["direction"], "up")
        self.assertEqual(aaa["strategy"], "bull_call_debit")
        # A session the calendar DID report on the previous day stays certain.
        bbb = kandidaten["BBB"]
        self.assertFalse(bbb["session_uncertain"])
        self.assertEqual(bbb["session"], "before_market")

    def test_an_invalid_day_is_a_client_error(self):
        for query in ("?day=kein-datum", "?day=2026-02-30", "", "?day="):
            with self.subTest(query=query):
                kalender = _calendar()
                antwort = _client(kalender).get(f"/api/v1/gap-scan{query}")
                self.assertEqual(antwort.status_code, 400)
                self.assertEqual(antwort.get_json(), {"error": "invalid_day"})
                # Validated BEFORE the source: a bad day costs no query.
                self.assertEqual(kalender.calls, 0)

    def test_an_unavailable_calendar_is_a_server_error(self):
        antwort, koerper, _kalender, _quelle = _scan(calendar=_calendar(state="unavailable"))
        self.assertEqual(antwort.status_code, 503)
        self.assertEqual(koerper["error"], "calendar_unavailable")

        # No source configured is the same answer: there is no candidate set.
        antwort, koerper, _kalender, _quelle = _scan(calendar=_calendar(state="not_configured"))
        self.assertEqual(antwort.status_code, 503)
        self.assertEqual(koerper["error"], "calendar_unavailable")
        self.assertEqual(koerper["source_code"], "not_configured")

        # An exploding adapter is an outage, not a traceback.
        antwort, koerper, _kalender, _quelle = _scan(calendar=_calendar(boom=True))
        self.assertEqual(antwort.status_code, 503)
        self.assertEqual(koerper["error"], "calendar_unavailable")

        # A blocked day names what the source DID instead of "did not answer".
        blockiert = _calendar(state="unavailable", error_by_day={
            DAY: {"code": "source_blocked", "message": "HTTP 403", "http_status": 403},
        })
        antwort, koerper, _kalender, _quelle = _scan(calendar=blockiert)
        self.assertEqual(antwort.status_code, 503)
        self.assertEqual(koerper["error"], "calendar_unavailable")
        self.assertEqual(koerper["source_code"], "source_blocked")
        self.assertEqual(koerper["http_status"], 403)
        self.assertIn("403", koerper["message"])

    def test_every_symbol_failing_still_answers(self):
        kaputt = _History(errors={
            symbol: GapSourceError("http_error", "the source answered HTTP 429", 429)
            for symbol in ("AAA", "BBB", "CCC")
        })
        antwort, koerper, _kalender, quelle = _scan(history=kaputt)

        # An answer, not an error: the run happened and produced nothing.
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(koerper["candidates"], [])
        self.assertEqual(koerper["counts"]["selected"], 0)
        self.assertEqual(koerper["counts"]["gaps_computed"], 0)
        self.assertEqual(koerper["counts"]["candidate_rows"], 3)
        self.assertEqual(koerper["counts"]["failures"], 3)
        self.assertEqual(sorted(koerper["failures"]), ["AAA", "BBB", "CCC"])
        self.assertEqual(set(koerper["failures"].values()), {"http_error"})
        self.assertEqual(sorted(quelle.calls), ["AAA", "BBB", "CCC"])

    def test_a_stale_day_is_not_upgraded_to_live(self):
        stale = _calendar(state_by_day={DAY: "stale"})
        antwort, koerper, _kalender, _quelle = _scan(calendar=stale)

        self.assertEqual(antwort.status_code, 200)
        # The reaction day's own state, passed through - old rows are old.
        self.assertEqual(koerper["day_state"], "stale")
        self.assertEqual(koerper["counts"]["selected"], 3)

    def test_a_monday_reaction_day_finds_fridays_reports(self):
        # The bug this pins: "one calendar day back" is Sunday after a Monday,
        # and Nasdaq answers a Sunday with an empty rows list - so every Friday
        # report after the close would be missing from a 200 that looks quiet.
        monday = date(2026, 11, 9)
        friday = "2026-11-06"
        kalender = _FakeCalendar(rows_by_day={
            friday: [calendar_row("FRI", session=None)],
            "2026-11-09": [calendar_row("MON", session="before_market")],
        })
        quelle = _History(bars={
            "FRI": bars(12.0, reaction=monday),
            "MON": bars(9.0, reaction=monday),
        })

        antwort = _client(kalender, quelle, now_fn=MONDAY_CLOCK).get(
            "/api/v1/gap-scan?day=2026-11-09"
        )
        koerper = antwort.get_json()

        self.assertEqual(antwort.status_code, 200)
        # Asked for the FRIDAY, in one calendar pair - not for the Sunday.
        angefragt = kalender.last_days or {}
        self.assertEqual(angefragt[DAY_PREV], friday)
        self.assertNotIn("2026-11-08", angefragt.values())
        kandidaten = _by_symbol(koerper["candidates"])
        self.assertEqual(sorted(kandidaten), ["FRI", "MON"])
        self.assertEqual(kandidaten["FRI"]["earnings_date"], friday)
        self.assertEqual(koerper["counts"]["candidate_rows"], 2)
        self.assertEqual(koerper["failures"], {})

    def test_a_holiday_is_not_the_previous_day(self):
        # 2026-11-27 is the session after Thanksgiving: the day before it is a
        # holiday, so the previous session is the Wednesday (25th), not the 26th.
        kalender = _FakeCalendar(rows_by_day={
            "2026-11-25": [calendar_row("WED", session=None)],
            "2026-11-27": [],
        })

        antwort = _client(kalender, _History(), now_fn=HOLIDAY_CLOCK).get(
            "/api/v1/gap-scan?day=2026-11-27"
        )

        self.assertEqual(antwort.status_code, 200)
        angefragt = kalender.last_days or {}
        self.assertEqual(angefragt[DAY_PREV], "2026-11-25")
        # Neither the holiday (26th) nor the Sunday (22nd) is ever asked for.
        self.assertNotIn("2026-11-26", angefragt.values())
        self.assertNotIn("2026-11-22", angefragt.values())

    def test_today_after_the_close_can_be_scanned(self):
        # The evening scan of the day that just closed: 22:30 CET is 16:30 ET,
        # and 16:00 ET itself is the close, not "still running". Both are 200.
        heute, gestern = "2026-11-12", "2026-11-11"
        for uhr, now_fn in (("16:00 ET", CLOCK), ("16:30 ET", AFTER_CLOSE_CLOCK)):
            with self.subTest(uhr=uhr):
                kalender = _FakeCalendar(rows_by_day={
                    gestern: [calendar_row("BBB", session="before_market")],
                    heute: [calendar_row("CCC", session="before_market")],
                })
                quelle = _History(bars={
                    "BBB": bars(10.4, reaction=date(2026, 11, 12)),
                    "CCC": bars(9.0, reaction=date(2026, 11, 12)),
                })

                antwort = _client(kalender, quelle, now_fn=now_fn).get(
                    f"/api/v1/gap-scan?day={heute}"
                )

                self.assertEqual(antwort.status_code, 200)
                koerper = antwort.get_json()
                self.assertEqual(koerper["day"], heute)
                # The reaction day is measured against the session before it.
                self.assertEqual(sorted((kalender.last_days or {}).values()), [gestern, heute])
                self.assertEqual(koerper["counts"]["candidate_rows"], 2)
                self.assertEqual(koerper["counts"]["selected"], 2)

    def test_today_before_the_close_is_refused(self):
        # 19:00 CET is 13:00 ET on the 12th: same date, but the session has no
        # close yet - a 400, and it costs no source call.
        kalender = _calendar()
        quelle = _history()

        antwort = _client(kalender, quelle, now_fn=BEFORE_CLOSE_CLOCK).get(
            "/api/v1/gap-scan?day=2026-11-12"
        )

        self.assertEqual(antwort.status_code, 400)
        koerper = antwort.get_json()
        self.assertEqual(koerper["error"], "invalid_day")
        self.assertEqual(koerper["message"], "the reaction session has not closed yet")
        self.assertEqual(kalender.calls, 0)
        self.assertEqual(quelle.calls, [])

    def test_a_future_day_is_refused(self):
        # The 13th is the day after the pinned clock: no session at all, and a
        # different reason than "today, still open".
        kalender = _calendar()
        quelle = _history()

        antwort = _client(kalender, quelle).get("/api/v1/gap-scan?day=2026-11-13")

        self.assertEqual(antwort.status_code, 400)
        koerper = antwort.get_json()
        self.assertEqual(koerper["error"], "invalid_day")
        self.assertEqual(koerper["message"], "the reaction day lies in the future")
        self.assertEqual(kalender.calls, 0)
        self.assertEqual(quelle.calls, [])

    def test_a_second_request_reuses_the_history(self):
        transport = _CountingTransport()
        with mock.patch("paperbroker.gap_scan.requests_transport", transport):
            # No gap_adapter on purpose: create_app builds the shared one.
            client = create_app(
                quote_adapter=object(), earnings_adapter=_calendar(), now_fn=CLOCK
            ).test_client()
            erster = client.get(f"/api/v1/gap-scan?day={DAY}")
            nach_erstem = len(transport.calls)
            zweiter = client.get(f"/api/v1/gap-scan?day={DAY}")

        self.assertEqual(erster.status_code, 200)
        self.assertEqual(zweiter.status_code, 200)
        # Three titles, three history requests - the first run really asked.
        self.assertEqual(nach_erstem, 3)
        self.assertEqual(erster.get_json()["failures"], {})
        # The second run asked for NOTHING: the shared adapter's cache answered.
        self.assertEqual(len(transport.calls), nach_erstem)
        self.assertEqual(zweiter.get_json()["counts"]["gaps_computed"], 3)
