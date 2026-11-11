"""The one age rule: stamps, zones, states and the two modes.

Pinned here because the rule decides money: whether a contract reaches the
result, whether a page calls the data "old", and whether an answer is allowed
to look like a current market. Every case is a value measured against a frozen
clock - no network, no real time.

What the old code did wrong (measured 2026-11-11 with arrow 0.10):
``arrow.get("2026-11-11 16:11:19")`` returns tzinfo=tzutc(), so ``.to("UTC")``
converted nothing while looking like a conversion. The same CBOE stamp was
"3 minutes old" for the filter and "zone unknown" for the quality gate. The
tests below pin that an unzoned stamp is reported as an ASSUMPTION
(``zone="assumed_utc"``, ``age_proven=False``, no UTC value) while a stamp that
carries ``Z`` or an offset is reported as a stated fact.
"""

import unittest
from datetime import UTC, datetime

import arrow

from paperbroker import quote_age
from paperbroker.assets import asset_factory
from paperbroker.quotes import OptionQuote
from paperbroker.screener import ScreenerCriteria, filter_stale, screen

#: A Wednesday, 14:35 UTC = 10:35 New York - the cash session is open.
CLOCK = datetime(2026, 11, 11, 14, 35, tzinfo=UTC)


def quote(stamp, bid=2.40, ask=2.50, iv=30.0, oi=1500, volume=300):
    """An OptionQuote carrying exactly the given feed stamp."""
    q = OptionQuote(quote_date="2026-11-11 14:30:00", asset=asset_factory("AAPL261120C00245000"),
                    price=(bid + ask) / 2, bid=bid, ask=ask, bid_size=100, ask_size=100,
                    delta=50, iv=iv, gamma=0.5, vega=5.0, theta=-3.0, rho=0.5,
                    underlying_price=250.0, open_interest=oi, volume=volume,
                    greeks_source="adapter")
    q.quote_timestamp = stamp
    return q


class TestTheStampIsReadNotGuessed(unittest.TestCase):
    """A zone either stands in the value or it does not - never in between."""

    def test_an_unzoned_stamp_is_an_assumption_not_a_fact(self):
        alter = quote_age.assess("2026-11-11 14:30:00", CLOCK, 30)
        self.assertEqual(alter.state, quote_age.STATE_FRESH)
        self.assertEqual(alter.zone, quote_age.ZONE_ASSUMED_UTC)
        self.assertFalse(alter.age_proven)          # 5 min ... under an assumption
        self.assertIsNone(alter.source_timestamp_utc)   # no UTC claim is made
        self.assertEqual(alter.source_timestamp_raw, "2026-11-11 14:30:00")
        self.assertAlmostEqual(alter.age_minutes, 5.0)

    def test_a_z_stamp_states_the_zone_and_earns_a_utc_value(self):
        alter = quote_age.assess("2026-11-11T14:30:00Z", CLOCK, 30)
        self.assertEqual(alter.zone, quote_age.ZONE_STATED)
        self.assertTrue(alter.age_proven)
        self.assertEqual(alter.source_timestamp_utc, "2026-11-11T14:30:00Z")

    def test_an_offset_is_converted_before_the_age_is_computed(self):
        # 16:30+02:00 is 14:30 UTC -> 5 min old, NOT 125 min and not -115
        alter = quote_age.assess("2026-11-11T16:30:00+02:00", CLOCK, 30)
        self.assertEqual(alter.zone, quote_age.ZONE_STATED)
        self.assertAlmostEqual(alter.age_minutes, 5.0)
        self.assertEqual(alter.source_timestamp_utc, "2026-11-11T14:30:00Z")
        self.assertEqual(alter.state, quote_age.STATE_FRESH)

    def test_a_date_without_a_time_is_not_an_age(self):
        # '2026-11-11' would previously become midnight UTC and read as 14.5 h old
        alter = quote_age.assess("2026-11-11", CLOCK, 30)
        self.assertEqual(alter.state, quote_age.STATE_UNREADABLE)
        self.assertIsNone(alter.age_minutes)
        self.assertEqual(alter.reason, quote_age.REASON_UNREADABLE)

    def test_a_date_that_does_not_exist_is_refused(self):
        alter = quote_age.assess("2026-02-30 10:00:00", CLOCK, 30)
        self.assertEqual(alter.state, quote_age.STATE_UNREADABLE)

    def test_junk_and_an_empty_value_keep_their_own_labels(self):
        # an empty value is NO stamp (missing), not a stamp we cannot read
        for wert in ("", "   "):
            with self.subTest(wert=wert):
                self.assertEqual(quote_age.assess(wert, CLOCK, 30).state,
                                 quote_age.STATE_MISSING)
        for wert in ("gestern abend", "14:35:00", "2026-11-11 25:00:00"):
            with self.subTest(wert=wert):
                self.assertEqual(quote_age.assess(wert, CLOCK, 30).state,
                                 quote_age.STATE_UNREADABLE)
        self.assertEqual(quote_age.assess(None, CLOCK, 30).state, quote_age.STATE_MISSING)
        self.assertEqual(quote_age.assess(None, CLOCK, 30).reason, quote_age.REASON_MISSING)

    def test_an_arrow_value_is_read_like_its_text(self):
        alter = quote_age.assess(arrow.get("2026-11-11T14:30:00Z"), CLOCK, 30)
        self.assertEqual(alter.zone, quote_age.ZONE_STATED)
        self.assertAlmostEqual(alter.age_minutes, 5.0)


class TestTheStates(unittest.TestCase):

    def test_the_limit_is_inclusive_and_keeps_the_rounding_of_the_old_text(self):
        # the old wording is a contract with the page: 30 min + 1 s reads "30 Min."
        self.assertEqual(quote_age.assess("2026-11-11 14:05:00", CLOCK, 30).state,
                         quote_age.STATE_FRESH)
        alt = quote_age.assess("2026-11-11 14:04:59", CLOCK, 30)
        self.assertEqual(alt.state, quote_age.STATE_STALE)
        self.assertEqual(alt.reason, "veraltet: 30 Min.")

    def test_a_future_stamp_is_named_after_the_tolerance(self):
        self.assertEqual(quote_age.assess("2026-11-11 14:35:59", CLOCK, 30).state,
                         quote_age.STATE_FRESH)          # 59 s ahead: clock skew
        weit = quote_age.assess("2026-11-12 14:35:00", CLOCK, 30)
        self.assertEqual(weit.state, quote_age.STATE_FUTURE)
        self.assertEqual(weit.reason, "Zeitstempel in der Zukunft (+1440 Min.)")

    def test_no_limit_means_unchecked_not_fresh(self):
        alter = quote_age.assess("2026-10-01 10:00:00", CLOCK, None)
        self.assertEqual(alter.state, quote_age.STATE_UNCHECKED)
        self.assertIsNone(alter.reason)
        self.assertIsNotNone(alter.age_minutes)          # the age is still reported

    def test_a_naive_clock_is_read_as_utc_and_junk_is_a_clock_error(self):
        self.assertEqual(quote_age.clock_utc(lambda: datetime(2026, 11, 11, 14, 35)),
                         CLOCK)
        for kaputt in (lambda: None, lambda: "2026-11-11T14:35:00Z", lambda: 42):
            with self.subTest(kaputt=kaputt):
                # a clock that answers nonsense is a CLOCK error, and the filter
                # names it as one - never as an unreadable stamp (B-17)
                with self.assertRaises((TypeError, AttributeError, ValueError)):
                    quote_age.clock_utc(kaputt)


class TestTheTwoModes(unittest.TestCase):
    """Both modes, on old / missing / unreadable / future stamps."""

    def _chain(self, stamp, oi=1500):
        return [quote(stamp, oi=oi)]

    def test_the_default_mode_drops_an_old_quote_with_its_reason(self):
        criteria = ScreenerCriteria(max_quote_age_min=30)
        self.assertEqual(criteria.data_mode, quote_age.MODE_FRESHNESS)
        kept, dropped = filter_stale(self._chain("2026-11-11 12:00:00"), criteria,
                                     now_fn=lambda: CLOCK)
        self.assertEqual(kept, [])
        self.assertEqual(dropped[0][1], "veraltet: 155 Min.")

    def test_the_preparation_mode_keeps_an_old_quote(self):
        criteria = ScreenerCriteria(max_quote_age_min=30,
                                    data_mode=quote_age.MODE_LAST_AVAILABLE)
        kept, dropped = filter_stale(self._chain("2026-11-11 12:00:00"), criteria,
                                     now_fn=lambda: CLOCK)
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, [])

    def test_the_preparation_mode_does_not_admit_undated_data(self):
        """Older data may be used; data whose state is unknown may not.

        The mode says "last available market state", not "any price we found".
        Missing, unreadable and future stamps keep their own reason in BOTH
        modes - that is the protection this mode must not remove.
        """
        criteria = ScreenerCriteria(max_quote_age_min=30,
                                    data_mode=quote_age.MODE_LAST_AVAILABLE)
        faelle = {
            None: "Aktualität unbekannt (kein Zeitstempel)",
            "gestern abend": "Aktualität unbekannt (Zeitstempel unlesbar)",
            "2026-11-13 14:35:00": "Zeitstempel in der Zukunft (+2880 Min.)",
        }
        for stamp, erwartet in faelle.items():
            with self.subTest(stamp=stamp):
                kept, dropped = filter_stale(self._chain(stamp), criteria,
                                             now_fn=lambda: CLOCK)
                self.assertEqual(kept, [])
                self.assertEqual([r for _q, r in dropped], [erwartet])

    def test_the_preparation_mode_keeps_every_other_filter_active(self):
        """The mode relaxes the AGE rule only.

        Same chain, same OI floor, only the mode differs: the OI filter removes
        the thin contract in BOTH modes, and the age rule is the only thing the
        mode touches.
        """
        alt = "2026-11-11 12:00:00"
        kette = [quote(alt, bid=5.00, ask=5.10, iv=28.0, oi=8000),
                 quote(alt, bid=2.40, ask=2.50, iv=28.0, oi=1500)]
        vorbereitung = ScreenerCriteria(max_quote_age_min=30, min_oi=2000,
                                        data_mode=quote_age.MODE_LAST_AVAILABLE)
        treffer, dropped = screen(kette, vorbereitung, now_fn=lambda: CLOCK)
        self.assertEqual(1, len(treffer))            # the thin one is gone ...
        self.assertEqual({r for _q, r in dropped}, {"OI 1500 < 2000"})   # ... by OI, not by age
        # the same chain and floor in the default mode: the age rule removes it all
        standard, dropped = screen(kette, ScreenerCriteria(max_quote_age_min=30, min_oi=2000),
                                   now_fn=lambda: CLOCK)
        self.assertEqual([], standard)
        self.assertEqual({r for _q, r in dropped}, {"veraltet: 155 Min."})

    def test_an_unknown_mode_is_refused_with_a_readable_reason(self):
        with self.assertRaises(ValueError) as ctx:
            ScreenerCriteria(data_mode="irgendwas").validate()
        self.assertIn("data_mode", str(ctx.exception))
        self.assertIn("last_available", str(ctx.exception))


class TestTheSharedStatusBlock(unittest.TestCase):

    def _block(self, mode, stamp="2026-11-11 12:00:00", **kwargs):
        ages = quote_age.assess_chain([quote(stamp)], CLOCK, kwargs.pop("max_age", 30))
        return quote_age.summarise(ages, mode=mode,
                                   max_age_min=kwargs.pop("max_age", 30), clock=CLOCK)

    def test_the_preparation_mode_marks_old_data_as_provisional(self):
        block = self._block(quote_age.MODE_LAST_AVAILABLE)
        self.assertTrue(block["provisional"])
        self.assertEqual(block["mode_label"], "Letzter verfügbarer Marktstand")
        # fetch time and source time are two different facts, both reported
        self.assertEqual(block["fetched_at_utc"], "2026-11-11T14:35:00Z")
        self.assertEqual(block["source_timestamp_raw"], "2026-11-11 12:00:00")
        self.assertIsNone(block["source_timestamp_utc"])
        self.assertEqual(block["source_timestamp_zone"], quote_age.ZONE_ASSUMED_UTC)
        self.assertIn("ohne Zeitzonenangabe", block["source_timestamp_zone_note"])
        self.assertFalse(block["age_proven"])
        self.assertEqual(block["counts"]["stale"], 1)
        # the sentence must not claim tradability
        self.assertIn("nicht ein aktueller Kurs", block["provisional_note"])
        self.assertNotIn("handelbar", block["provisional_note"])

    def test_the_default_mode_is_never_provisional(self):
        block = self._block(quote_age.MODE_FRESHNESS)
        self.assertFalse(block["provisional"])
        self.assertIsNone(block["provisional_note"])
        self.assertEqual(block["mode_label"], "Aktualitätsfilter")

    def test_fresh_data_is_not_dressed_up_as_provisional(self):
        block = self._block(quote_age.MODE_LAST_AVAILABLE, stamp="2026-11-11 14:30:00")
        self.assertFalse(block["provisional"])       # nothing old to warn about
        self.assertEqual(block["counts"]["fresh"], 1)

    def test_undated_data_counts_as_unproven(self):
        ages = quote_age.assess_chain([quote(None), quote("gestern abend")], CLOCK, 30)
        block = quote_age.summarise(ages, mode=quote_age.MODE_LAST_AVAILABLE,
                                    max_age_min=30, clock=CLOCK)
        self.assertEqual(block["counts"]["undated"], 2)
        self.assertTrue(block["provisional"])
        self.assertIsNone(block["source_timestamp_raw"])   # no stamp to show


class TestTheMarketState(unittest.TestCase):
    """Closed-market detection uses the calendar this project already has."""

    def test_a_wednesday_inside_the_session_is_open(self):
        markt = quote_age.market_state(CLOCK)          # 10:35 New York
        self.assertEqual(markt["state"], quote_age.MARKET_OPEN)
        self.assertEqual(markt["exchange_day"], "2026-11-11")

    def test_before_and_after_the_session_and_the_weekend(self):
        vor = quote_age.market_state(datetime(2026, 11, 11, 13, 0, tzinfo=UTC))
        self.assertEqual((vor["state"], vor["reason"]), (quote_age.MARKET_CLOSED, "before_open"))
        nach = quote_age.market_state(datetime(2026, 11, 11, 21, 30, tzinfo=UTC))
        self.assertEqual((nach["state"], nach["reason"]), (quote_age.MARKET_CLOSED, "after_close"))
        samstag = quote_age.market_state(datetime(2026, 11, 14, 16, 0, tzinfo=UTC))
        self.assertEqual((samstag["state"], samstag["reason"]),
                         (quote_age.MARKET_CLOSED, "weekend"))
        self.assertEqual(quote_age.market_reason_text(samstag["reason"]), "Wochenende")

    def test_a_nyse_holiday_comes_from_the_existing_calendar(self):
        # Thanksgiving 2026 is Thursday 26 Nov - 16:00 UTC would be business hours
        feiertag = quote_age.market_state(datetime(2026, 11, 26, 16, 0, tzinfo=UTC))
        self.assertEqual((feiertag["state"], feiertag["reason"]),
                         (quote_age.MARKET_CLOSED, "holiday"))

    def test_the_provisional_note_names_a_closed_market(self):
        samstag = datetime(2026, 11, 14, 16, 0, tzinfo=UTC)
        ages = quote_age.assess_chain([quote("2026-11-13 20:00:00")], samstag, 30)
        block = quote_age.summarise(ages, mode=quote_age.MODE_LAST_AVAILABLE,
                                    max_age_min=30, clock=samstag)
        self.assertTrue(block["provisional"])
        self.assertIn("Markt geschlossen (Wochenende)", block["note"])


if __name__ == "__main__":
    unittest.main()
