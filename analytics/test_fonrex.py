"""The Fonrex source: what it fills, what it leaves alone, and what it says when it cannot answer."""

import os
import unittest
from unittest import mock

import fonrex
import store


def yearly(period, currency="EUR", **figures):
    return {"period_end": period, "currency": currency,
            "figures": {name: {"value": value, "unit": currency if "eps" not in name else currency + "/shares"}
                        for name, value in figures.items()}}


class EsefTests(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"pb-fonrex-{id(self)}.db")
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.tmp + suffix)
            except OSError:
                pass
        self.addCleanup(self.cleanup)
        patcher = mock.patch.dict(os.environ, {"AKTIENANALYSE_FONREX_KEY": "test-only",
                                               "AKTIENANALYSE_DB": self.tmp})
        patcher.start()
        self.addCleanup(patcher.stop)
        fonrex.store = store

    def cleanup(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.tmp + suffix)
            except OSError:
                pass

    def test_the_annual_figures_arrive_in_the_vocabulary_of_the_page(self):
        answer = {"years": [yearly("2024-12-31", revenue=84_683_000_000, net_income=12_550_000_000,
                                  eps_diluted=25.12, total_equity=67_517_000_000,
                                  operating_cashflow=18_924_000_000)]}
        with mock.patch.object(fonrex, "_get", return_value=answer):
            sections, currency, reason = fonrex.esef_sections("MC.PA", "2026-11-20")
        self.assertIsNone(reason)
        self.assertEqual("EUR", currency)
        row = sections["income"]["2024-12-31"]
        self.assertAlmostEqual(84_683_000_000, row["totalrevenue"], places=2)
        self.assertAlmostEqual(12_550_000_000, row["netincome"], places=2)
        self.assertAlmostEqual(25.12, row["dilutedeps"], places=4)
        self.assertAlmostEqual(67_517_000_000, sections["balance"]["2024-12-31"]["stockholdersequity"], places=2)
        self.assertAlmostEqual(18_924_000_000,
                               sections["cash"]["2024-12-31"]["totalcashfromoperatingactivities"], places=2)

    def test_a_year_after_the_cutoff_stays_out(self):
        answer = {"years": [yearly("2025-12-31", revenue=1.0), yearly("2024-12-31", revenue=2.0)]}
        with mock.patch.object(fonrex, "_get", return_value=answer):
            sections, _currency, _reason = fonrex.esef_sections("ASML.AS", "2024-12-31")
        self.assertEqual(["2024-12-31"], sorted(sections["income"]))

    def test_an_empty_answer_is_named_not_hidden(self):
        with mock.patch.object(fonrex, "_get", return_value={"years": []}) as asked:
            sections, currency, reason = fonrex.esef_sections("SAP.DE", "2026-11-20")
        self.assertEqual(({}, None, fonrex.NO_REPORTS), (sections, currency, reason))
        self.assertEqual(1, asked.call_count)

    def test_the_reason_for_german_issuers_names_the_esef_gap_not_the_source(self):
        """Fonrex serves German issuers; only its ESEF index is empty for them."""
        with mock.patch.object(fonrex, "_get", return_value={"years": []}):
            _sections, _currency, reason = fonrex.esef_sections("SAP.DE", "2026-11-20")
        self.assertIn("ESEF", reason or "")
        self.assertIn("Kennzahlen", reason or "")

    def test_an_unreachable_source_says_so(self):
        with mock.patch.object(fonrex, "_get", return_value=None):
            sections, currency, reason = fonrex.esef_sections("SIE.DE", "2026-11-20")
        self.assertEqual({}, sections)
        self.assertEqual(fonrex.UNREACHABLE, reason)

    def test_the_second_look_does_not_ask_again(self):
        answer = {"years": [yearly("2024-12-31", revenue=5.0)]}
        with mock.patch.object(fonrex, "_get", return_value=answer) as asked:
            fonrex.esef_sections("MC.PA")
            fonrex.esef_sections("MC.PA")
        self.assertEqual(1, asked.call_count, "the stored answer is used while it is fresh")

    def test_without_a_key_nothing_is_asked(self):
        os.environ.pop("AKTIENANALYSE_FONREX_KEY", None)
        with mock.patch.object(fonrex, "_get") as asked:
            sections, _currency, reason = fonrex.esef_sections("MC.PA")
            self.assertIsNone(fonrex.risk("MC.PA"))
        self.assertEqual(({}, fonrex.NO_KEY), (sections, reason))
        self.assertEqual(0, asked.call_count)

    def test_a_source_that_delivered_is_not_called_unreachable(self):
        """The block only names a reason from the report path - never an invented one."""
        self.assertEqual([], fonrex.notes("SU.PA", "FR0000121972", None))
        self.assertEqual([], fonrex.notes("SU.PA", "FR0000121972", fonrex.NO_KEY))
        block = fonrex.notes("OR.PA", "FR0000120321", fonrex.NO_REPORTS)
        self.assertEqual("Fonrex", block[0]["block"])

    def test_a_risk_answer_is_passed_through_only_when_it_carries_a_figure(self):
        # The shape the service really answers with (measured 2026-11-22): the window is in it, and
        # `years` is not to be trusted - it said 3 for a window of one year.
        answer = {"ticker": "SAP.DE", "annualized_volatility": 0.4143, "returns": 252,
                  "max_drawdown": {"value": -0.3886, "peak": "2026-01-02", "trough": "2026-06-30"},
                  "years": 3, "start": "2025-11-21", "end": "2026-11-20"}
        with mock.patch.object(fonrex, "_get", return_value=answer):
            antwort = fonrex.risk("SAP.DE", "DE0007164600")
        self.assertIsNotNone(antwort)
        self.assertAlmostEqual(0.4143, (antwort or {}).get("annualized_volatility", 0))
        with mock.patch.object(fonrex, "_get", return_value={"ticker": "SAP.DE"}):
            fonrex.store = store
            with mock.patch.object(store, "read_fonrex", return_value=None):
                self.assertIsNone(fonrex.risk("OTHER.DE"))


class DefectTests(unittest.TestCase):
    """The six things Kody found on the first commit."""

    def setUp(self):
        self.tmp = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"pb-kody-{id(self)}.db")
        patcher = mock.patch.dict(os.environ, {"AKTIENANALYSE_FONREX_KEY": "test-only",
                                               "AKTIENANALYSE_FONREX_URL": "http://fonrex-api:5000",
                                               "AKTIENANALYSE_DB": self.tmp})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.tmp + suffix)
            except OSError:
                pass

    def test_the_provenance_map_survives_the_merge(self):
        """Kody: merge_fonrex dropped the key the page uses to mark a figure as tagged."""
        import app
        provenance = {"income": {"2024-12-31": {"totalrevenue": {"source": "ixbrl_original"}}}}
        merged = app.merge_fonrex({"income": {"2024-12-31": {"totalrevenue": 1.0}},
                                   "provenance": provenance},
                                  {"income": {"2025-12-31": {"totalrevenue": 2.0}}})
        self.assertEqual(provenance, merged["provenance"])

    def test_a_payload_of_the_wrong_shape_is_a_reason_not_an_exception(self):
        """Kody: a non-dict answer raised AttributeError inside the request thread."""
        for ticker, answer in (("AA.PA", ["unerwartet"]), ("BB.PA", "text")):
            with mock.patch.object(fonrex, "_get", return_value=answer):
                sections, _currency, reason = fonrex.esef_sections(ticker)
            self.assertEqual({}, sections)
            self.assertEqual(fonrex.UNREACHABLE, reason, "an unreadable answer is not 'no reports'")
        for ticker, answer in (("CC.PA", {"years": "keine Liste"}), ("DD.PA", {"years": [None, 7]}),
                               ("EE.PA", {"years": []})):
            with mock.patch.object(fonrex, "_get", return_value=answer):
                sections, _currency, reason = fonrex.esef_sections(ticker)
            self.assertEqual({}, sections)
            self.assertEqual(fonrex.NO_REPORTS, reason)

    def test_an_unreachable_service_is_asked_once_not_per_symbol(self):
        """Kody: three blocking timeouts per cold profile while the host is down."""
        with mock.patch.object(fonrex, "_get", return_value=None) as asked:
            fonrex.esef_sections("MC.PA")
            fonrex.esef_sections("SU.PA")
            fonrex.risk("MC.PA")
        self.assertEqual(1, asked.call_count, "one timeout, then the mark holds for five minutes")

    def test_the_key_does_not_travel_to_a_public_host_in_clear_text(self):
        """Kody: X-API-KEY over plain HTTP."""
        with mock.patch.object(fonrex, "BASE", "http://fonrex.example.com:5000"):
            self.assertFalse(fonrex.permitted_target())
            # The guard sits in _get, the one place that attaches the header.
            with mock.patch.object(fonrex, "urlopen", side_effect=AssertionError("must not connect")):
                self.assertIsNone(fonrex._get("/esef/MC.PA/statements"))
        for address in ("https://fonrex.example.com", "http://fonrex-api:5000",
                        "http://192.168.178.10:5100", "http://172.29.80.48:5000"):
            with mock.patch.object(fonrex, "BASE", address):
                self.assertTrue(fonrex.permitted_target(), address)
        for address in ("ftp://fonrex-api:5000", "http://8.8.8.8:5000"):
            with mock.patch.object(fonrex, "BASE", address):
                self.assertFalse(fonrex.permitted_target(), address)

    def test_an_insecure_address_is_named_and_remembers_nothing(self):
        """Kody: an address the key may not travel to was reported as a dead host."""
        with mock.patch.object(fonrex, "BASE", "http://fonrex.example.com:5000"):
            sections, _currency, reason = fonrex.esef_sections("MC.PA")
            self.assertEqual({}, sections)
            self.assertEqual(fonrex.INSECURE_TARGET, reason)
            from contextlib import closing
            with closing(store.connect()) as conn:
                self.assertIsNone(store.read_fonrex(conn, fonrex.DOWN_KEY, max_age_h=1),
                                  "nothing was attempted, so nothing may be remembered")

    def test_a_ticker_cannot_walk_out_of_its_path(self):
        """Kody: '..' passed the page's symbol pattern and reached the raw path."""
        for ticker in ("..", "../sec", ".", "", "A/B"):
            self.assertFalse(fonrex.symbol_ok(ticker), ticker)
            with mock.patch.object(fonrex, "_get") as asked:
                self.assertEqual({}, fonrex.esef_sections(ticker)[0])
                self.assertIsNone(fonrex.risk(ticker))
            self.assertEqual(0, asked.call_count)
        self.assertTrue(fonrex.symbol_ok("MC.PA"))
        self.assertTrue(fonrex.symbol_ok("BRK-B"))

    def test_a_404_for_one_issuer_does_not_silence_the_others(self):
        """Kody: a per-resource error marked the whole service dead for five minutes."""
        with mock.patch.object(fonrex, "_get", return_value=fonrex.HTTP_FAILED):
            sections, _currency, reason = fonrex.esef_sections("OR.PA")
            self.assertEqual({}, sections)
            self.assertEqual(fonrex.NO_REPORTS, reason, "a 404 is 'nothing there', not 'unreachable'")
            # the service still answers for the next issuer
            with mock.patch.object(fonrex, "_get",
                                   return_value={"years": [{"period_end": "2024-12-31", "currency": "EUR",
                                                            "figures": {"revenue": {"value": 5.0}}}]}):
                self.assertTrue(fonrex.esef_sections("MC.PA")[0])
        from contextlib import closing
        with closing(store.connect()) as conn:
            self.assertIsNone(store.read_fonrex(conn, fonrex.DOWN_KEY, max_age_h=1))

    def test_a_refused_key_is_its_own_state(self):
        """Kody: 401/403 was reported as 'this issuer has no reports'."""
        with mock.patch.object(fonrex, "_get", return_value=fonrex.AUTH_FAILED):
            sections, _currency, reason = fonrex.esef_sections("MC.PA")
        self.assertEqual({}, sections)
        self.assertEqual(fonrex.AUTH_REJECTED, reason)
        from contextlib import closing
        with closing(store.connect()) as conn:
            self.assertIsNone(store.read_fonrex(conn, fonrex.DOWN_KEY, max_age_h=1),
                              "a refused key is not an outage")

    def test_a_server_error_still_counts_as_unreachable(self):
        """Kody: 500/503 must not become 'no reports' either."""
        with mock.patch.object(fonrex, "_get", return_value=None):
            sections, _currency, reason = fonrex.esef_sections("MC.PA")
        self.assertEqual({}, sections)
        self.assertEqual(fonrex.UNREACHABLE, reason)

    def test_the_three_statuses_are_told_apart(self):
        """404 -> nothing there, 401 -> key refused, 503 -> service failing."""
        import io
        from urllib.error import HTTPError

        def answer(code):
            return HTTPError("http://fonrex-api:5000/x", code, "x", {}, io.BytesIO(b""))

        expectations = {404: fonrex.HTTP_FAILED, 401: fonrex.AUTH_FAILED, 403: fonrex.AUTH_FAILED,
                        500: None, 503: None}
        for code, expected in expectations.items():
            with mock.patch.object(fonrex, "urlopen", side_effect=answer(code)):
                got = fonrex._get("/esef/MC.PA/statements")
            self.assertIs(expected, got, f"HTTP {code}")

    def test_a_warm_answer_survives_while_the_service_is_marked_down(self):
        answer = {"years": [{"period_end": "2024-12-31", "currency": "EUR",
                            "figures": {"revenue": {"value": 7.0}}}]}
        with mock.patch.object(fonrex, "_get", return_value=answer):
            fonrex.esef_sections("MC.PA")
        store.write_fonrex(fonrex.DOWN_KEY, {"down": True})
        with mock.patch.object(fonrex, "_get", side_effect=AssertionError("must not be asked")):
            sections, _currency, _reason = fonrex.esef_sections("MC.PA")
        self.assertTrue(sections.get("income"), "the stored answer is still served")

    def test_a_year_without_known_figures_does_not_become_a_year(self):
        """Kody: an unmapped year appeared as a business year without revenue."""
        answer = {"years": [{"period_end": "2024-12-31", "currency": "EUR",
                             "figures": {"unbekannt": {"value": 1.0}}}]}
        with mock.patch.object(fonrex, "_get", return_value=answer):
            sections, _currency, reason = fonrex.esef_sections("MC.PA")
        self.assertEqual({}, sections)
        self.assertEqual(fonrex.NO_REPORTS, reason)


class MergeTests(unittest.TestCase):
    def test_a_tagged_figure_is_never_overwritten(self):
        import app
        statements = {"income": {"2024-12-31": {"totalrevenue": 1.0}}, "balance": {}, "cash": {}, "quarterly": {}}
        extra = {"income": {"2024-12-31": {"totalrevenue": 99.0, "netincome": 5.0}}}
        merged = app.merge_fonrex(statements, extra)
        self.assertEqual(1.0, merged["income"]["2024-12-31"]["totalrevenue"])
        self.assertEqual(5.0, merged["income"]["2024-12-31"]["netincome"])

    def test_a_year_already_there_is_enriched_not_added_twice(self):
        """A retailer's 2021-01-03 and a report's 2020-12-31 are the same business year."""
        import app
        statements = {"income": {"2021-01-03": {"totalrevenue": 74.0}}, "balance": {}, "cash": {}}
        extra = {"income": {"2020-12-31": {"totalrevenue": 99.0, "netincome": 2.0}}}
        merged = app.merge_fonrex(statements, extra)
        self.assertEqual(["2021-01-03"], sorted(merged["income"]), "one year, not two")
        self.assertEqual(74.0, merged["income"]["2021-01-03"]["totalrevenue"], "the tagged figure wins")
        self.assertEqual(2.0, merged["income"]["2021-01-03"]["netincome"], "the missing one is added")

    def test_a_year_two_years_apart_is_a_different_year(self):
        import app
        merged = app.merge_fonrex({"income": {"2021-01-03": {"totalrevenue": 74.0}}},
                                  {"income": {"2020-06-30": {"totalrevenue": 50.0}}})
        self.assertEqual(2, len(merged["income"]))

    def test_empty_sections_are_dropped(self):
        import app
        merged = app.merge_fonrex({}, {"income": {"2024-12-31": {"totalrevenue": 2.0}},
                                       "balance": {}, "cash": {}})
        self.assertEqual({"income"}, set(merged))
