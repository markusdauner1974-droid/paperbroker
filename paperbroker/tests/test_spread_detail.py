"""Offline tests for ``paperbroker.spread_detail`` - the arithmetic of ONE spread.

The module has no source and no clock: it takes two quotes, a day, a listed
expiry and a spot, and answers the position. Everything pinned here is read off
that answer against numbers computed by hand, not against a second copy of the
same formula:

* ``net_debit``/``max_loss``/``max_profit``/``breakeven``/``rr`` for a call AND
  for a put spread (the payout is one expression with two signs),
* the exact payout at expiration below, between and above the strikes,
* the model value today: negative at the spot (the debit was paid at the
  ask/bid, the model prices the mid) and never below the maximum loss,
* the series: 120..200 points from 50 % to 170 % of the spot, the spot itself
  among them, and a call curve that rises up to the upper strike,
* six scenarios whose underlying is the spot times the factor and whose
  ``pl_expiry`` is the exact payout,
* the two refusals: a leg without a readable quote is ``leg_not_listed``, a
  pair that costs nothing is ``invalid_spread`` - never a 200 with half a
  spread in it.
"""

import unittest
from datetime import date

from paperbroker import spread_detail as sd
from paperbroker.assets import asset_factory
from paperbroker.quotes import OptionQuote

DAY = date(2026, 11, 12)
EXPIRY = date(2027, 3, 19)
DAYS = (EXPIRY - DAY).days          # 127
SPOT = 100.0


def occ(symbol, expiry, option_type, strike):
    """A real OCC symbol; the asset factory parses it back."""
    year, month, day = str(expiry).split("-")
    kind = "C" if option_type == "call" else "P"
    return f"{symbol}{year[2:]}{month}{day}{kind}{int(round(strike * 1000)):08d}"


def quote(option_type, strike, bid, ask, *, iv=30.0, delta=None, gamma=1.0,
          vega=1.0, theta=-1.0, oi=1000, volume=100, underlying=SPOT):
    """One chain row as the adapter delivers it - greeks already in percent."""
    return OptionQuote(
        quote_date="2026-11-12 20:00:00",
        asset=asset_factory(occ("AAA", EXPIRY, option_type, strike)),
        price=(bid + ask) / 2.0,
        bid=bid, ask=ask, bid_size=50, ask_size=50,
        delta=0.0 if delta is None else delta, iv=iv, gamma=gamma, vega=vega,
        theta=theta, rho=0.1, underlying_price=underlying,
        open_interest=oi, volume=volume, greeks_source="adapter",
    )


def call_spread():
    """Bought 100 call at 7.05, sold 103.30 call at 5.60 -> debit 1.45."""
    return (quote("call", 100.0, 6.95, 7.05, iv=30.0, delta=52.0),
            quote("call", 103.3, 5.60, 5.70, iv=31.0, delta=28.0))


def put_spread():
    """Bought 100 put at 7.35, sold 96.70 put at 5.80 -> debit 1.55."""
    return (quote("put", 100.0, 7.25, 7.35, iv=30.0, delta=-52.0),
            quote("put", 96.7, 5.80, 5.90, iv=31.0, delta=-28.0))


def detail(long_quote, short_quote, option_type="call", strategy="bull_call_debit",
           label="Bull Call", spot=SPOT):
    return sd.build_detail(
        symbol="AAA", name="Alpha Corp", day=DAY, strategy=strategy,
        strategy_label=label, expiration=EXPIRY, option_type=option_type,
        long_quote=long_quote, short_quote=short_quote, spot=spot,
        spot_source="chain_underlying",
    )


class TestTheNumbersOfThePosition(unittest.TestCase):
    def test_a_call_spread_has_the_numbers_of_the_row(self):
        lang, kurz = call_spread()
        zahlen = sd.spread_numbers(lang, kurz, "call")
        # Hand: debit 7.05 - 5.60 = 1.45; width 3.30; profit (3.30-1.45)*100.
        self.assertEqual(zahlen["net_debit"], 1.45)
        self.assertEqual(zahlen["max_loss"], 145.0)
        self.assertEqual(zahlen["max_profit"], 185.0)
        self.assertEqual(zahlen["breakeven"], 101.45)
        self.assertEqual(zahlen["rr"], 1.28)

    def test_a_put_spread_has_the_numbers_of_the_row(self):
        lang, kurz = put_spread()
        zahlen = sd.spread_numbers(lang, kurz, "put")
        # Hand: debit 7.35 - 5.80 = 1.55; break-even 100 - 1.55.
        self.assertEqual(zahlen["net_debit"], 1.55)
        self.assertEqual(zahlen["max_loss"], 155.0)
        self.assertEqual(zahlen["max_profit"], 175.0)
        self.assertEqual(zahlen["breakeven"], 98.45)
        self.assertEqual(zahlen["rr"], 1.13)

    def test_the_whole_answer_carries_the_same_numbers(self):
        body = detail(*call_spread())
        self.assertEqual(body["net_debit"], 1.45)
        self.assertEqual(body["max_loss"], 145.0)
        self.assertEqual(body["max_profit"], 185.0)
        self.assertEqual(body["breakeven"], 101.45)
        self.assertEqual(body["rr"], 1.28)
        self.assertEqual(body["dte"], DAYS)
        self.assertEqual(body["net_debit_basis"], "ask_long_minus_bid_short")

    def test_the_position_greeks_are_long_minus_short(self):
        body = detail(*call_spread())
        self.assertEqual(body["position_greeks"]["delta"], 24.0)
        self.assertEqual(body["position_greeks"]["gamma"], 0.0)
        self.assertEqual(body["position_greeks"]["vega"], 0.0)
        self.assertEqual(body["position_greeks"]["theta"], 0.0)


class TestThePayoutAtExpiration(unittest.TestCase):
    def test_a_call_spread_pays_below_between_and_above_its_strikes(self):
        lang, kurz = call_spread()
        debit, kl, ks = 1.45, 100.0, 103.3
        # Below both strikes: the whole debit is lost.
        self.assertAlmostEqual(sd.expiry_pl("call", 90.0, kl, ks, debit), -145.0)
        # Between: the bought call is worth 1.00, still a loss of 0.45.
        self.assertAlmostEqual(sd.expiry_pl("call", 101.0, kl, ks, debit), -45.0)
        # Above both: the width minus the debit is the maximum.
        self.assertAlmostEqual(sd.expiry_pl("call", 110.0, kl, ks, debit), 185.0)

    def test_a_put_spread_is_the_same_expression_with_the_other_sign(self):
        lang, kurz = put_spread()
        debit, kl, ks = 1.55, 100.0, 96.7
        self.assertAlmostEqual(sd.expiry_pl("put", 90.0, kl, ks, debit), 175.0)
        self.assertAlmostEqual(sd.expiry_pl("put", 98.0, kl, ks, debit), 45.0)
        self.assertAlmostEqual(sd.expiry_pl("put", 110.0, kl, ks, debit), -155.0)

    def test_at_the_break_even_nothing_is_won_or_lost(self):
        lang, kurz = call_spread()
        self.assertAlmostEqual(sd.expiry_pl("call", 101.45, 100.0, 103.3, 1.45), 0.0)
        lang, kurz = put_spread()
        self.assertAlmostEqual(sd.expiry_pl("put", 98.45, 100.0, 96.7, 1.55), 0.0)


class TestTheModelCurve(unittest.TestCase):
    def test_today_at_the_spot_is_negative_and_above_the_maximum_loss(self):
        body = detail(*call_spread())
        heute = [point for point in body["series"] if point["price"] == SPOT][0]
        # The debit is paid at the ask/bid, the model prices the mid: the gap is
        # the half-spread the trade costs, and it is a small loss - not zero.
        self.assertLess(heute["pl_today"], 0.0)
        self.assertGreater(heute["pl_today"], -body["max_loss"])

    def test_the_model_curve_rises_with_the_underlying(self):
        serie = detail(*call_spread())["series"]
        werte = [point["pl_today"] for point in serie]
        for links, rechts in zip(werte, werte[1:], strict=False):
            self.assertLessEqual(links, rechts)
        # At 170 % of the spot both legs are deep in the money: the curve is
        # allowed to stay below the exact payout (the debit is already paid),
        # but it has to be there - not somewhere in the middle of the range.
        self.assertGreater(werte[-1], 0.9 * 185.0)

    def test_without_a_volatility_there_is_no_model_curve(self):
        self.assertIsNone(sd.today_pl("call", SPOT, 100.0, 103.3, 1.45, None, 31.0, 0.35))

    def test_a_call_is_worth_its_intrinsic_value_at_expiration(self):
        self.assertEqual(sd.black_scholes("call", 110.0, 100.0, 0.0, 0.3), 10.0)
        self.assertEqual(sd.black_scholes("call", 90.0, 100.0, 0.0, 0.3), 0.0)
        self.assertEqual(sd.black_scholes("put", 90.0, 100.0, 0.0, 0.3), 10.0)


class TestTheSeries(unittest.TestCase):
    def test_the_axis_runs_from_half_to_1_7_times_the_spot(self):
        serie = detail(*call_spread())["series"]
        self.assertGreaterEqual(len(serie), 120)
        self.assertLessEqual(len(serie), 200)
        self.assertAlmostEqual(serie[0]["price"], SPOT * 0.5, places=2)
        self.assertAlmostEqual(serie[-1]["price"], SPOT * 1.7, places=2)

    def test_the_spot_is_one_of_the_points(self):
        serie = detail(*call_spread())["series"]
        self.assertIn(SPOT, [point["price"] for point in serie])

    def test_a_call_curve_rises_up_to_the_upper_strike(self):
        serie = detail(*call_spread())["series"]
        bis_short = [p["pl_expiry"] for p in serie if p["price"] <= 103.3]
        for links, rechts in zip(bis_short, bis_short[1:], strict=False):
            self.assertLessEqual(links, rechts)
        # and it flattens above it - the width is the whole gain
        ueber_short = {p["pl_expiry"] for p in serie if p["price"] >= 103.3}
        self.assertEqual(ueber_short, {185.0})

    def test_a_put_curve_falls_towards_the_upper_strike(self):
        serie = detail(*put_spread(), option_type="put",
                       strategy="bear_put_debit", label="Bear Put")["series"]
        bis_long = [p["pl_expiry"] for p in serie if p["price"] <= 96.7]
        self.assertEqual(set(bis_long), {175.0})
        letzter = serie[-1]["pl_expiry"]
        self.assertEqual(letzter, -155.0)


class TestTheScenarios(unittest.TestCase):
    def test_six_steps_around_the_spot(self):
        szenarien = detail(*call_spread())["scenarios"]
        self.assertEqual([s["change_pct"] for s in szenarien], [-30, -20, -10, 0, 10, 20])
        for szenario in szenarien:
            self.assertEqual(szenario["underlying"],
                             round(SPOT * (1 + szenario["change_pct"] / 100.0), 2))

    def test_the_payout_of_every_scenario_is_exact(self):
        szenarien = detail(*call_spread())["scenarios"]
        erwartet = {
            -30: -145.0,   # 70.00 - below both strikes
            -20: -145.0,   # 80.00
            -10: -145.0,   # 90.00
            0: -145.0,     # 100.00 - at the long strike, still the debit
            10: 185.0,     # 110.00 - above the short strike
            20: 185.0,     # 120.00
        }
        for szenario in szenarien:
            self.assertEqual(szenario["pl_expiry"], erwartet[szenario["change_pct"]])

    def test_the_scenario_greeks_follow_the_model(self):
        szenarien = detail(*call_spread())["scenarios"]
        # A call spread's delta falls as the underlying rises above the short
        # strike: the bought leg is done, the sold leg is not.
        self.assertGreater(szenarien[3]["delta"], szenarien[5]["delta"])
        for szenario in szenarien:
            for name in ("delta", "gamma", "vega", "theta"):
                self.assertIsInstance(szenario[name], float)

    def test_the_model_greeks_are_of_the_agreed_scale(self):
        # delta/gamma in percent per 100 shares, vega per volatility point,
        # theta per calendar day: an at-the-money call is near 50 delta.
        lang = sd.model_greeks("call", SPOT, 100.0, DAYS / 365.0, 0.30)
        self.assertAlmostEqual(lang["delta"], 55.0, delta=4.0)
        self.assertGreater(lang["vega"], 0.0)
        self.assertLess(lang["theta"], 0.0)
        put = sd.model_greeks("put", SPOT, 100.0, DAYS / 365.0, 0.30)
        self.assertAlmostEqual(put["delta"], lang["delta"] - 100.0, delta=0.5)


class TestTheRefusals(unittest.TestCase):
    def test_a_leg_without_an_ask_is_not_a_spread(self):
        lang, kurz = call_spread()
        lang.ask = None
        with self.assertRaises(sd.SpreadDetailError) as fehler:
            sd.build_detail(symbol="AAA", name="Alpha", day=DAY, strategy="bull_call_debit",
                            strategy_label="Bull Call", expiration=EXPIRY,
                            option_type="call", long_quote=lang, short_quote=kurz,
                            spot=SPOT, spot_source="chain_underlying")
        self.assertEqual(fehler.exception.code, "leg_not_listed")

    def test_a_pair_that_costs_nothing_is_refused(self):
        with self.assertRaises(sd.SpreadDetailError) as fehler:
            sd.spread_numbers(quote("call", 100.0, 3.00, 3.00),
                              quote("call", 103.3, 3.00, 3.00), "call")
        self.assertEqual(fehler.exception.code, "invalid_spread")

    def test_two_legs_of_one_strike_are_refused(self):
        with self.assertRaises(sd.SpreadDetailError) as fehler:
            sd.spread_numbers(quote("call", 100.0, 6.95, 7.05),
                              quote("call", 100.0, 5.60, 5.70), "call")
        self.assertEqual(fehler.exception.code, "invalid_spread")


if __name__ == "__main__":
    unittest.main()
