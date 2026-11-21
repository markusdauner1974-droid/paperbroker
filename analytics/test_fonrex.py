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

    def test_a_risk_answer_is_passed_through_only_when_it_carries_a_figure(self):
        answer = {"ticker": "SAP.DE", "annualized_volatility": 0.4143,
                  "max_drawdown": {"value": -0.3886}, "years": 3}
        with mock.patch.object(fonrex, "_get", return_value=answer):
            antwort = fonrex.risk("SAP.DE", "DE0007164600")
        self.assertIsNotNone(antwort)
        self.assertAlmostEqual(0.4143, (antwort or {}).get("annualized_volatility", 0))
        with mock.patch.object(fonrex, "_get", return_value={"ticker": "SAP.DE"}):
            fonrex.store = store
            with mock.patch.object(store, "read_fonrex", return_value=None):
                self.assertIsNone(fonrex.risk("OTHER.DE"))


class MergeTests(unittest.TestCase):
    def test_a_tagged_figure_is_never_overwritten(self):
        import app
        statements = {"income": {"2024-12-31": {"totalrevenue": 1.0}}, "balance": {}, "cash": {}, "quarterly": {}}
        extra = {"income": {"2024-12-31": {"totalrevenue": 99.0, "netincome": 5.0}}}
        merged = app.merge_fonrex(statements, extra)
        self.assertEqual(1.0, merged["income"]["2024-12-31"]["totalrevenue"])
        self.assertEqual(5.0, merged["income"]["2024-12-31"]["netincome"])

    def test_empty_sections_are_dropped(self):
        import app
        merged = app.merge_fonrex({}, {"income": {"2024-12-31": {"totalrevenue": 2.0}},
                                       "balance": {}, "cash": {}})
        self.assertEqual({"income"}, set(merged))
