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
* seven scenarios whose underlying is the spot times the factor and whose
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
    def test_seven_steps_around_the_spot(self):
        szenarien = detail(*call_spread())["scenarios"]
        self.assertEqual([s["change_pct"] for s in szenarien], [-30, -20, -10, 0, 10, 20, 30])
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
            30: 185.0,     # 130.00 - above the short strike, capped
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


class TestTheGreekCurves(unittest.TestCase):
    """The four model greeks per series point, and the seven value markers.

    The page draws ONE greek over the two P&L curves from these numbers; it does
    not compute anything itself. Two things therefore have to hold: every point
    of the series carries the four values, and the seven scenario prices are
    points of that same series - otherwise a marker would float beside the curve
    or show a number that disagrees with the table below it.
    """

    def test_every_point_carries_the_four_greeks(self):
        for punkt in detail(*call_spread())["series"]:
            self.assertEqual(set(punkt), {"price", "pl_expiry", "pl_today",
                                          "delta", "gamma", "vega", "theta"})
            for name in sd.GREEKS:
                self.assertIsInstance(punkt[name], float)
                # Vier Dezimalen: die Kurve braucht sie, die Anzeige rundet.
                self.assertEqual(punkt[name], round(punkt[name], 4))

    def test_the_greek_curve_is_not_quantised(self):
        # Zwei Dezimalen waeren als Kurve eine Treppe: Theta liegt zwischen
        # -0,10 und +0,30, das sind 41 Stufen fuer 166 Punkte. Gemessen wird
        # hier die Zahl der verschiedenen Werte - sie muss in die Naehe der
        # Punktzahl kommen, sonst steht die Kurve wieder in Stufen da.
        body = detail(*call_spread())
        for name in sd.GREEKS:
            werte = [punkt[name] for punkt in body["series"]]
            self.assertGreater(len(set(werte)), len(werte) * 0.9,
                               f"{name} ist quantisiert: {len(set(werte))} von {len(werte)}")

    def test_the_seven_scenario_prices_are_points_of_the_series(self):
        body = detail(*call_spread())
        preise = {punkt["price"] for punkt in body["series"]}
        for szenario in body["scenarios"]:
            self.assertIn(szenario["underlying"], preise)

    def test_a_marker_and_its_table_cell_are_the_same_number(self):
        # One computation, two roundings: the series keeps four decimals for the
        # curve, the table shows two. What a reader sees is the same number.
        body = detail(*call_spread())
        je_preis = {punkt["price"]: punkt for punkt in body["series"]}
        for szenario in body["scenarios"]:
            punkt = je_preis[szenario["underlying"]]
            for name in sd.GREEKS:
                self.assertEqual(round(punkt[name], 2), szenario[name], name)
            self.assertEqual(punkt["pl_expiry"], szenario["pl_expiry"])

    def test_the_delta_at_the_spot_is_the_model_delta_of_the_position(self):
        # Recomposed from the single-option greeks, not from the helper the
        # series uses: the bought leg minus the sold one, each at ITS own vol.
        body = detail(*call_spread())
        punkt = [p for p in body["series"] if p["price"] == SPOT][0]
        jahre = DAYS / 365.0
        lang = sd.model_greeks("call", SPOT, 100.0, jahre, 0.30, digits=4)
        kurz = sd.model_greeks("call", SPOT, 103.3, jahre, 0.31, digits=4)
        links, rechts = lang["delta"], kurz["delta"]
        assert isinstance(links, float) and isinstance(rechts, float)
        self.assertEqual(punkt["delta"], round(links - rechts, 4))
        # Ein Call-Spread ist positiv und deutlich unter 100 Delta.
        self.assertGreater(punkt["delta"], 0.0)
        self.assertLess(punkt["delta"], 100.0)

    def test_a_put_spread_has_a_negative_delta_over_the_whole_axis(self):
        body = detail(*put_spread(), option_type="put", strategy="bear_put_debit",
                      label="Bear Put")
        for punkt in body["series"]:
            self.assertLess(punkt["delta"], 0.0)
            self.assertIsInstance(punkt["gamma"], float)
        # Eine EINZELNE Option hat nie negatives Gamma. Die Differenz zweier
        # Legs kann dagegen um eine Hundertstel kippen, weil die beiden Legs
        # zwei verschiedene Volatilitaeten tragen (-0.01 gemessen) - deshalb
        # steht hier die einzelne Option, nicht die Position.
        einzel = sd.model_greeks("put", 95.0, 96.7, DAYS / 365.0, 0.31)["gamma"]
        assert einzel is not None
        self.assertGreaterEqual(einzel, 0.0)

    def test_a_sub_dollar_spot_keeps_the_scenario_prices_on_the_axis(self):
        # Unter einem Dollar faellt das Gitter auf vier Dezimalen (sonst
        # kollabiert die Achse). Die Szenariopreise muessen trotzdem exakt die
        # Punkte bleiben, die die Tabelle zeigt - sonst sitzt der Marker neben
        # der Kurve und die Zelle nennt einen Preis, den die Kurve nicht hat.
        spot = 0.85
        lang = quote("call", spot, 0.10, 0.12, iv=40.0)
        kurz = quote("call", round(spot * 1.03, 2), 0.05, 0.07, iv=41.0)
        body = detail(lang, kurz, spot=spot)
        vierstellig = [p["price"] for p in body["series"] if round(p["price"], 2) != p["price"]]
        self.assertTrue(vierstellig, "das Raster muss hier vierstellig sein")
        self.assertGreaterEqual(len(body["series"]), 120)
        preise = {p["price"]: p for p in body["series"]}
        for szenario in body["scenarios"]:
            self.assertEqual(szenario["underlying"],
                             round(spot * (1 + szenario["change_pct"] / 100.0), 2))
            self.assertIn(szenario["underlying"], preise)
            punkt = preise[szenario["underlying"]]
            for name in sd.GREEKS:
                self.assertEqual(round(punkt[name], 2), szenario[name], name)

    def test_the_explanation_numbers_come_with_the_answer(self):
        # Die Legende erklaert die aktive Metrik mit Zahlen aus der Antwort -
        # 25 Delta heisst 25 Cent je Dollar, dafuer braucht es keine zweite Zahl.
        body = detail(*call_spread())
        erklaerung = body["greek_explain"]
        punkt = [p for p in body["series"] if p["price"] == SPOT][0]
        self.assertEqual(erklaerung["delta_value"], int(round(punkt["delta"])))
        self.assertLessEqual(abs(erklaerung["delta_value"] - punkt["delta"]), 0.5)
        self.assertEqual(erklaerung["theta_value"], round(punkt["theta"], 2))
        self.assertEqual(erklaerung["theta_cents"],
                         int(round(abs(punkt["theta"]) * 100.0)))

    def test_without_a_volatility_the_explanation_has_no_number(self):
        lang, kurz = call_spread()
        lang.iv = None
        erklaerung = detail(lang, kurz)["greek_explain"]
        self.assertIsNone(erklaerung["delta_value"])
        self.assertIsNone(erklaerung["theta_value"])
        self.assertIsNone(erklaerung["theta_cents"])

    def test_without_a_volatility_the_greeks_stay_none(self):
        lang, kurz = call_spread()
        lang.iv = None
        body = detail(lang, kurz)
        for punkt in body["series"]:
            for name in sd.GREEKS:
                self.assertIsNone(punkt[name])


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
