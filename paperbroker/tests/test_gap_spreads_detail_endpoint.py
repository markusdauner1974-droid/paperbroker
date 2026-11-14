"""Offline tests for one spread's detail endpoint (GET /api/v1/gap-spreads/detail).

The list route answers WHICH spread a candidate trades; this route answers what
that one spread looks like. Both derive the spread through the same stages, so
the fixtures here are the funnel's own (the calendar rows, the daily bars, the
chain and the report date of ``test_gap_funnel``) - the contract of the detail
answer is what this file pins:

* the whole documented shape, every number at two decimals, and no exception
  text on a 200,
* ``net_debit``/``max_loss``/``max_profit``/``breakeven``/``rr`` of the agreed
  row, read back from the answer,
* 120..200 series points that include the spot, six scenarios whose underlying
  is the spot times the factor,
* the position greeks as long leg minus short leg,
* the error side: 404 ``chain_unavailable`` when the chain cannot be read, 404
  ``leg_not_listed`` when a leg of the row is not in the chain, 404
  ``unknown_symbol`` for a title that has no row that day, 400 ``invalid_day``,
* no source call for a request that is already invalid.

No test touches a network: all four sources are fakes handed to ``create_app``.
"""

import unittest
from datetime import date
from unittest import mock

import arrow

from paperbroker import gap_funnel
from paperbroker.gap_scan import GapSourceError
from paperbroker.server import create_app
from paperbroker.tests.test_gap_funnel import (
    DAY,
    PREVIOUS,
    REACTION,
    _bars,
    _Chains,
    _FakeCalendar,
    _History,
    _NextEarnings,
    calendar_row,
    ladder_chain,
)

CLOCK = lambda: arrow.get("2026-11-14T21:00:00Z")  # noqa: E731

DETAIL = "/api/v1/gap-spreads/detail"

#: The whole answer, in the order of the agreed contract.
DETAIL_KEYS = {
    "schema_version", "symbol", "name", "day", "strategy", "strategy_label",
    "expiration", "dte", "spot", "spot_source", "strikes", "legs", "net_debit",
    "net_debit_basis", "max_loss", "max_profit", "breakeven", "rr",
    "position_greeks", "series", "scenarios", "notes",
}
LEG_KEYS = {"side", "strike", "bid", "ask", "mid", "iv", "delta", "gamma", "vega",
            "theta", "open_interest", "volume"}
SCENARIO_KEYS = {"change_pct", "underlying", "pl_expiry", "delta", "gamma", "vega", "theta"}


def _calendar():
    """AAA reported after the PREVIOUS close, DDD only after the reaction day."""
    return _FakeCalendar({
        PREVIOUS: [calendar_row("AAA", session="after_market", name="Alpha Corp")],
        DAY: [calendar_row("DDD", session="after_market")],
    })


def _history():
    return _History({"AAA": _bars(110.0)})


def _dates():
    return _NextEarnings({"AAA": date(2027, 2, 1)})


def _chains(chain=None):
    return _Chains({"AAA": ladder_chain("AAA") if chain is None else chain})


def _client(chains=None, dates=None, calendar=None, history=None):
    return create_app(
        quote_adapter=chains if chains is not None else _chains(),
        earnings_adapter=calendar or _calendar(),
        gap_adapter=history or _history(),
        next_earnings_adapter=dates or _dates(),
        now_fn=CLOCK,
    ).test_client()


def _get(query=f"?symbol=AAA&day={DAY}", **kwargs):
    antwort = _client(**kwargs).get(DETAIL + query)
    return antwort, antwort.get_json()


class DetailEndpointTest(unittest.TestCase):
    def test_the_json_contract(self):
        antwort, body = _get()
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(set(body), DETAIL_KEYS)
        self.assertEqual(body["schema_version"], "1.0")
        self.assertEqual(body["symbol"], "AAA")
        self.assertEqual(body["name"], "Alpha Corp")
        self.assertEqual(body["day"], DAY)
        self.assertEqual(body["strategy"], "bull_call_debit")
        self.assertEqual(body["strategy_label"], "Bull Call Spread (Debit)")
        self.assertEqual(body["expiration"], "2027-03-19")
        self.assertEqual(body["dte"], (date(2027, 3, 19) - REACTION).days)
        self.assertEqual(set(body["strikes"]), {"long", "short"})
        self.assertEqual([leg["side"] for leg in body["legs"]], ["long", "short"])
        for leg in body["legs"]:
            self.assertEqual(set(leg), LEG_KEYS)
        for scenario in body["scenarios"]:
            self.assertEqual(set(scenario), SCENARIO_KEYS)
        self.assertIsInstance(body["notes"], list)
        self.assertTrue(all(isinstance(note, str) for note in body["notes"]))

    def test_the_numbers_are_the_agreed_row(self):
        _antwort, body = _get()
        # ladder_chain: bought 100 call at 4.00, sold 103.30 call at 3.00.
        self.assertEqual(body["net_debit_basis"], "ask_long_minus_bid_short")
        self.assertEqual(body["net_debit"], 1.00)
        self.assertEqual(body["max_loss"], 100.00)
        self.assertEqual(body["max_profit"], 230.00)
        self.assertEqual(body["breakeven"], 101.00)
        self.assertEqual(body["rr"], 2.3)
        self.assertEqual(body["strikes"], {"long": 100.0, "short": 103.3})

    def test_every_number_carries_two_decimals(self):
        _antwort, body = _get()

        def floats(value):
            if isinstance(value, dict):
                for item in value.values():
                    yield from floats(item)
            elif isinstance(value, list):
                for item in value:
                    yield from floats(item)
            elif isinstance(value, float):
                yield value

        for number in floats(body):
            self.assertEqual(number, round(number, 2), f"{number} has more than two decimals")

    def test_no_exception_text_reaches_a_200(self):
        _antwort, body = _get()
        text = str(body).lower()
        for verboten in ("traceback", "exception", "error", "<class"):
            self.assertNotIn(verboten, text)

    def test_the_legs_carry_the_chain_numbers(self):
        _antwort, body = _get()
        lang, kurz = body["legs"]
        self.assertEqual((lang["strike"], lang["bid"], lang["ask"], lang["mid"]),
                         (100.0, 3.9, 4.0, 3.95))
        self.assertEqual(lang["iv"], 30.0)
        self.assertEqual(lang["delta"], 55.0)
        self.assertEqual(lang["open_interest"], 1200)
        self.assertEqual(kurz["strike"], 103.3)
        self.assertEqual(kurz["delta"], 30.0)
        # Position greeks: long minus short, in the chain's convention.
        self.assertEqual(body["position_greeks"]["delta"], 25.0)
        self.assertEqual(body["position_greeks"]["gamma"], 0.0)

    def test_the_series_covers_the_agreed_axis_and_contains_the_spot(self):
        _antwort, body = _get()
        serie = body["series"]
        self.assertGreaterEqual(len(serie), 120)
        self.assertLessEqual(len(serie), 200)
        preise = [point["price"] for point in serie]
        self.assertEqual(preise, sorted(preise))
        self.assertAlmostEqual(preise[0], 50.0, places=2)
        self.assertAlmostEqual(preise[-1], 170.0, places=2)
        self.assertIn(body["spot"], preise)
        for point in serie:
            self.assertEqual(set(point), {"price", "pl_expiry", "pl_today"})

    def test_the_scenarios_are_six_steps_around_the_spot(self):
        _antwort, body = _get()
        szenarien = body["scenarios"]
        self.assertEqual([s["change_pct"] for s in szenarien], [-30, -20, -10, 0, 10, 20])
        for szenario in szenarien:
            self.assertAlmostEqual(szenario["underlying"],
                                   body["spot"] * (1 + szenario["change_pct"] / 100.0),
                                   places=2)
        # At the lower end the call spread is worth nothing: the whole debit.
        self.assertEqual(szenarien[0]["pl_expiry"], -body["max_loss"])
        # And above the upper strike it is worth the width: the maximum.
        self.assertEqual(szenarien[-1]["pl_expiry"], body["max_profit"])

    def test_the_spot_comes_from_the_chain_and_is_named(self):
        _antwort, body = _get()
        self.assertEqual(body["spot"], 100.0)
        self.assertEqual(body["spot_source"], "chain_underlying")

    def test_without_an_underlying_price_the_close_of_the_row_is_the_spot(self):
        kette = ladder_chain("AAA")
        for quote in kette:
            quote.underlying_price = None
        _antwort, body = _get(chains=_Chains({"AAA": kette}))
        self.assertEqual(body["spot"], 110.0)
        self.assertEqual(body["spot_source"], "row_close")


class DetailErrorTest(unittest.TestCase):
    def test_a_chain_that_cannot_be_read_is_404_chain_unavailable(self):
        antwort, body = _get(chains=_Chains({"AAA": GapSourceError("blocked", "the chain is blocked")}))
        self.assertEqual(antwort.status_code, 404)
        self.assertEqual(body["error"], "chain_unavailable")
        self.assertIn("message", body)

    def test_a_leg_that_is_not_in_the_chain_is_404_leg_not_listed(self):
        # The row names a short strike the chain does not hold: the answer may
        # not price half a spread, so it names the missing leg instead.
        def fake_run(candidates, *, fetch_chain, **_kwargs):
            fetch_chain(candidates[0]["symbol"])
            lauf = gap_funnel.FunnelRun(counts={})
            lauf.results.append({
                "symbol": "AAA", "name": "Alpha Corp", "gap_pct": 10.0,
                "direction": "up", "strategy": "bull_call_debit",
                "strategy_label": "Bull Call", "close": 110.0,
                "earnings_date": PREVIOUS, "next_earnings_date": "2027-02-01",
                "expiration": "2027-03-19", "dte": 127,
                "long_strike": 100.0, "short_strike": 999.0,
                "net_debit": 1.0, "max_loss": 100.0, "max_profit": 230.0, "rr": 2.3,
            })
            return lauf

        with mock.patch.object(gap_funnel, "run_funnel", side_effect=fake_run):
            antwort, body = _get()
        self.assertEqual(antwort.status_code, 404)
        self.assertEqual(body["error"], "leg_not_listed")
        self.assertIn("999", body["message"])

    def test_an_unknown_symbol_is_404_and_costs_no_chain(self):
        chains = _chains()
        antwort, body = _get("?symbol=ZZZZ&day=" + DAY, chains=chains)
        self.assertEqual(antwort.status_code, 404)
        self.assertEqual(body["error"], "unknown_symbol")
        self.assertEqual(chains.calls, [])

    def test_an_invalid_day_is_400_and_costs_no_source_call(self):
        calendar, history, chains, dates = _calendar(), _history(), _chains(), _dates()
        antwort, body = _get("?symbol=AAA&day=2026-13-40",
                             chains=chains, calendar=calendar,
                             history=history, dates=dates)
        self.assertEqual(antwort.status_code, 400)
        self.assertEqual(body["error"], "invalid_day")
        self.assertEqual((calendar.calls, history.calls, chains.calls, dates.calls),
                         (0, [], [], []))

    def test_a_missing_symbol_parameter_is_404_unknown_symbol(self):
        antwort, body = _get(f"?day={DAY}")
        self.assertEqual(antwort.status_code, 404)
        self.assertEqual(body["error"], "unknown_symbol")

    def test_a_second_parameter_is_refused(self):
        antwort, body = _get(f"?symbol=AAA&day={DAY}&long_strike=100")
        self.assertEqual(antwort.status_code, 400)
        self.assertEqual(body["error"], "unknown_parameter")


if __name__ == "__main__":
    unittest.main()
