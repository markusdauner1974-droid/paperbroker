"""The hand calendar inside the funnel: who fills a gap, and who never may.

The rules are strict and this file pins each of them with its own test:

* a curated date is read ONLY where the source answered "no date" - a source
  date always wins, and a covered title keeps its old answer,
* an expired entry is no date: the title stays in the exclusions and says
  ``expired`` instead of pretending to have a report,
* the field appears only for a title the pre-selection lets through: with no
  options list nothing is shown (an unloaded list must not look like "no
  options"), and a title without options is not offered hand work,
* a title with a source date never costs a feed request.
"""

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

from paperbroker import hand_calendar as hc
from paperbroker.gap_funnel import REASON_NO_NEXT_EARNINGS, run_funnel
from paperbroker.tests.test_gap_funnel import NOW, REACTION, candidate, ladder_chain

KURATIERT = date(2027, 2, 1)


class _Feed:
    """A feed fake: answers what it was told and remembers who asked."""

    def __init__(self, signal=None):
        self.signal_value = signal
        self.asked = []

    def signal(self, name, **_kwargs):
        self.asked.append(name)
        return self.signal_value


SIGNAL = {"title": "Acme Inc. to Report Q4 Earnings on February 1 - Reuters",
          "link": "https://news.example.com/acme"}


def _store(entries=None, day=REACTION):
    tmp = tempfile.mkdtemp()
    pfad = os.path.join(tmp, "hand.json")
    Path(pfad).write_text(json.dumps({"version": 1, "symbols": entries or {}}),
                          encoding="utf-8")
    return hc.HandCalendar(path=pfad)


def _run(*, next_earnings=None, entries=None, feed=None, optionable=frozenset({"AAA"}),
         chain=None):
    """One title through the funnel with an injected store, feed and layer."""
    return run_funnel(
        [candidate("AAA")],
        fetch_next_earnings=lambda _symbol: next_earnings,
        fetch_chain=lambda _symbol: list(chain if chain is not None else ladder_chain()),
        day=REACTION, now_fn=NOW,
        hand_calendar=_store(entries),
        feed=feed,
        optionable=optionable,
    )


class CuratedDateTest(unittest.TestCase):
    def test_a_curated_date_lets_the_title_run_the_whole_funnel(self):
        lauf = _run(entries={"AAA": {"date": "2027-02-01", "source": "IR-Pressemitteilung",
                                     "notiert_am": "2026-11-17"}})
        self.assertEqual([], lauf.excluded)
        self.assertEqual(1, len(lauf.results))
        zeile = lauf.results[0]
        # Der Termin steht da - aber er sagt, woher er kommt.
        self.assertEqual("2027-02-01", zeile["next_earnings_date"])
        self.assertEqual("manual_curated", zeile["next_earnings_source"])
        self.assertEqual("IR-Pressemitteilung", zeile["hand_calendar"]["source"])
        self.assertEqual("2026-11-17", zeile["hand_calendar"]["notiert_am"])
        self.assertEqual("curated", zeile["hand_calendar"]["status"])
        # Und die Kette laeuft weiter: der Verfall liegt NACH dem Bericht.
        self.assertGreater(zeile["expiration"], "2027-02-01")

    def test_a_source_date_wins_over_a_curated_one(self):
        lauf = _run(next_earnings=date(2027, 1, 5),
                    entries={"AAA": {"date": "2027-02-01", "source": "IR"}})
        zeile = lauf.results[0]
        self.assertEqual("2027-01-05", zeile["next_earnings_date"])
        self.assertEqual("finnhub", zeile["next_earnings_source"])
        self.assertNotIn("hand_calendar", zeile)

    def test_an_expired_entry_is_no_date(self):
        lauf = _run(entries={"AAA": {"date": "2026-11-10", "source": "IR",
                                     "notiert_am": "2026-10-01"}})
        self.assertEqual([], lauf.results)
        ausschluss = lauf.excluded[0]
        self.assertEqual(REASON_NO_NEXT_EARNINGS, ausschluss.reason)
        self.assertEqual("expired", ausschluss.hand_calendar["status"])
        self.assertEqual("2026-11-10", ausschluss.hand_calendar["curated_date"])

    def test_without_a_curated_date_the_title_stays_excluded(self):
        lauf = _run()
        self.assertEqual(REASON_NO_NEXT_EARNINGS, lauf.excluded[0].reason)
        # Die Schicht laesst den Titel durch, also steht der Satz da, dass der
        # Feed nichts hergibt - kein Feld, aber auch kein Schweigen.
        self.assertEqual("no_signal", lauf.excluded[0].hand_calendar["status"])


class PreSelectionTest(unittest.TestCase):
    def test_an_optionable_title_with_a_signal_gets_the_field(self):
        feed = _Feed(SIGNAL)
        lauf = _run(feed=feed)
        block = lauf.excluded[0].hand_calendar
        self.assertEqual("signal", block["status"])
        self.assertEqual(SIGNAL["title"], block["title"])
        self.assertEqual(SIGNAL["link"], block["link"])
        self.assertIsNone(block["curated_date"])
        # Die Suche laeuft mit dem NAMEN, nicht nur dem Ticker.
        self.assertEqual(["AAA Inc."], feed.asked)

    def test_an_optionable_title_without_a_signal_says_so(self):
        lauf = _run(feed=_Feed(None))
        self.assertEqual("no_signal", lauf.excluded[0].hand_calendar["status"])
        self.assertIsNone(lauf.excluded[0].hand_calendar["title"])

    def test_without_a_symbol_list_no_field_is_offered(self):
        feed = _Feed(SIGNAL)
        lauf = _run(feed=feed, optionable=None)
        self.assertIsNone(lauf.excluded[0].hand_calendar)
        self.assertEqual([], feed.asked, "ohne Liste wird nicht einmal gefragt")

    def test_a_title_without_options_is_not_offered_hand_work(self):
        feed = _Feed(SIGNAL)
        lauf = _run(feed=feed, optionable=frozenset({"OTHER"}))
        self.assertIsNone(lauf.excluded[0].hand_calendar)
        self.assertEqual([], feed.asked)

    def test_without_a_feed_the_field_is_absent_but_the_layer_still_runs(self):
        lauf = _run(feed=None)
        self.assertEqual("no_signal", lauf.excluded[0].hand_calendar["status"])

    def test_an_expired_entry_and_a_signal_show_both(self):
        lauf = _run(feed=_Feed(SIGNAL),
                    entries={"AAA": {"date": "2026-11-10", "source": "IR",
                                     "notiert_am": "2026-10-01"}})
        block = lauf.excluded[0].hand_calendar
        self.assertEqual("expired", block["status"])
        self.assertEqual(SIGNAL["link"], block["link"])
        self.assertEqual("2026-11-10", block["curated_date"])


class ContractTest(unittest.TestCase):
    def test_the_exclusion_carries_the_block_by_name(self):
        lauf = _run(feed=_Feed(SIGNAL))
        dikt = lauf.excluded[0].as_dict()
        self.assertIn("hand_calendar", dikt)
        self.assertEqual({"status", "title", "link", "curated_date", "source", "notiert_am"},
                         set(dikt["hand_calendar"]))
