"""Offline tests for the EVA data-quality gate.

Pure logic plus one route-level check. No network, no clock: every timestamp is
a literal and every "now" is passed in, so each expectation is readable next to
the assertion.

The first case is the bug this module exists for: CBOE delivers
"2026-10-24 14:30:00" with no timezone, and the screener used to read it as UTC.
"""

import json
import unittest
from datetime import UTC, datetime, timedelta, timezone

import arrow

from paperbroker.eva_quality import (
    SCHEMA_VERSION,
    STATUS_DISABLED,
    STATUS_INVALID,
    STATUS_OK,
    STATUS_STALE,
    STATUS_UNKNOWN,
    assess_source_quality,
    snapshot_identity,
)
from paperbroker.server import create_app
from paperbroker.tests.test_screener import make, quote
from paperbroker.tests.test_server import _clock, _MockAdapter

NOW = datetime(2026, 10, 24, 14, 35, tzinfo=UTC)
LIMIT = 30 * 60


def _assess(**kwargs):
    parameter = {
        "source_timestamp": "2026-10-24T14:30:00+00:00",
        "now": NOW,
        "max_age_seconds": LIMIT,
        "multiplier": 100,
    }
    parameter.update(kwargs)
    return assess_source_quality(**parameter)


def _codes(ergebnis):
    return [grund["code"] for grund in ergebnis["reasons"]]


class TestUnknownSourceTimezone(unittest.TestCase):
    """The documented gap: a naive source stamp must never count as fresh."""

    def test_a_cboe_stamp_without_timezone_is_never_ok(self):
        # Exactly the reproduction: 14:30 source, 14:35 check, 30 min limit.
        # The old filter read the naive stamp as UTC and kept the contract.
        ergebnis = assess_source_quality(
            source_timestamp="2026-10-24 14:30:00",
            source_timezone=None,
            now=NOW,
            max_age_seconds=LIMIT,
            multiplier=None,
        )
        self.assertEqual(ergebnis["status"], STATUS_UNKNOWN)
        self.assertFalse(ergebnis["eligible"])
        self.assertIn("source_timezone_unproven", _codes(ergebnis))
        self.assertIsNone(ergebnis["snapshot"]["source_timestamp_utc"])
        self.assertIsNone(ergebnis["snapshot"]["source_age_seconds"])

    def test_a_naive_stamp_with_a_proven_utc_zone_is_ok(self):
        # "Proven" means the caller states the zone; the module does not guess.
        ergebnis = _assess(source_timestamp="2026-10-24 14:30:00", source_timezone="UTC")
        self.assertEqual(ergebnis["status"], STATUS_OK)
        self.assertTrue(ergebnis["eligible"])
        self.assertEqual(ergebnis["snapshot"]["source_timezone"], "UTC")

    def test_an_iana_zone_is_not_applied_to_a_naive_stamp(self):
        # Applying America/New_York to a naive stamp is ambiguous twice a year,
        # so the zone counts as unproven instead of being guessed.
        ergebnis = _assess(source_timestamp="2026-10-24 14:30:00",
                           source_timezone="America/New_York")
        self.assertEqual(ergebnis["status"], STATUS_UNKNOWN)
        self.assertIn("source_timezone_unproven", _codes(ergebnis))

    def test_an_explicit_offset_is_a_proof(self):
        ergebnis = _assess(source_timestamp="2026-10-24T16:30:00+02:00")
        self.assertEqual(ergebnis["status"], STATUS_OK)
        self.assertEqual(ergebnis["snapshot"]["source_age_seconds"], 300.0)
        self.assertEqual(ergebnis["snapshot"]["source_timezone"], "+02:00")


class TestMissingAndBrokenTimestamps(unittest.TestCase):
    def test_a_missing_stamp_is_unknown(self):
        for wert in (None, "", "   "):
            with self.subTest(wert=wert):
                ergebnis = _assess(source_timestamp=wert)
                self.assertEqual(ergebnis["status"], STATUS_UNKNOWN)
                self.assertIn("source_timestamp_missing", _codes(ergebnis))
                self.assertFalse(ergebnis["eligible"])

    def test_an_unparseable_stamp_is_invalid(self):
        for wert in ("kaputt", "2026-13-45T10:00:00", "24.10.2026 14:30"):
            with self.subTest(wert=wert):
                ergebnis = _assess(source_timestamp=wert)
                self.assertEqual(ergebnis["status"], STATUS_INVALID)
                self.assertIn("source_timestamp_invalid", _codes(ergebnis))
                self.assertFalse(ergebnis["eligible"])

    def test_fetched_at_does_not_replace_a_source_stamp(self):
        # Our own fetch time proves when we asked, not when the source measured.
        ergebnis = _assess(source_timestamp=None, fetched_at=NOW.isoformat())
        self.assertEqual(ergebnis["status"], STATUS_UNKNOWN)
        self.assertIn("source_timestamp_missing", _codes(ergebnis))
        self.assertIn("fetched_at_is_not_a_source_time", _codes(ergebnis))
        self.assertFalse(ergebnis["eligible"])

    def test_a_naive_check_clock_is_refused(self):
        # A naive "now" would smuggle in the very assumption this module bans.
        ergebnis = _assess(now=datetime(2026, 10, 24, 14, 35))
        self.assertEqual(ergebnis["status"], STATUS_INVALID)
        self.assertIn("check_clock_missing", _codes(ergebnis))


class TestAge(unittest.TestCase):
    def test_an_old_stamp_is_stale(self):
        ergebnis = _assess(source_timestamp="2026-10-24T13:00:00+00:00")
        self.assertEqual(ergebnis["status"], STATUS_STALE)
        self.assertFalse(ergebnis["eligible"])
        self.assertEqual(ergebnis["snapshot"]["source_age_seconds"], 5700.0)

    def test_a_stamp_in_the_future_beyond_tolerance_is_invalid(self):
        ergebnis = _assess(source_timestamp="2026-10-24T16:00:00+00:00")
        self.assertEqual(ergebnis["status"], STATUS_INVALID)
        self.assertIn("source_timestamp_in_future", _codes(ergebnis))

    def test_clock_skew_of_seconds_is_tolerated(self):
        ergebnis = _assess(source_timestamp="2026-10-24T14:35:30+00:00")
        self.assertEqual(ergebnis["status"], STATUS_OK)

    def test_the_limit_is_inclusive_like_the_screener(self):
        ergebnis = _assess(source_timestamp="2026-10-24T14:05:00+00:00")
        self.assertEqual(ergebnis["snapshot"]["source_age_seconds"], 1800.0)
        self.assertEqual(ergebnis["status"], STATUS_OK)


class TestDisabledCheck(unittest.TestCase):
    def test_a_disabled_age_check_is_its_own_state_and_never_a_pass(self):
        ergebnis = _assess(max_age_seconds=None)
        self.assertEqual(ergebnis["status"], STATUS_DISABLED)
        self.assertFalse(ergebnis["eligible"])
        self.assertIn("age_check_disabled", _codes(ergebnis))
        self.assertTrue(ergebnis["checks"]["age_check_disabled"])

    def test_a_disabled_check_still_reports_missing_proofs(self):
        # ignore_quote_age=1 must not silence the other questions.
        ergebnis = assess_source_quality(
            source_timestamp="2026-10-24 14:30:00", now=NOW,
            max_age_seconds=None, multiplier=None,
        )
        self.assertFalse(ergebnis["eligible"])
        self.assertIn("source_timezone_unproven", _codes(ergebnis))
        self.assertIn("contract_multiplier_unknown", _codes(ergebnis))
        self.assertIn("age_check_disabled", _codes(ergebnis))


class TestContractMetadata(unittest.TestCase):
    def test_a_missing_multiplier_is_named_not_defaulted(self):
        ergebnis = _assess(multiplier=None)
        self.assertEqual(ergebnis["status"], STATUS_UNKNOWN)
        self.assertFalse(ergebnis["eligible"])
        self.assertIn("contract_multiplier_unknown", _codes(ergebnis))
        self.assertIsNone(ergebnis["contract"]["multiplier"])
        self.assertFalse(ergebnis["contract"]["multiplier_proven"])

    def test_an_unusable_multiplier_is_invalid(self):
        for wert in (0, -5, 0.0, True, "100"):
            with self.subTest(wert=wert):
                ergebnis = _assess(multiplier=wert)
                self.assertEqual(ergebnis["status"], STATUS_INVALID)
                self.assertIn("contract_multiplier_invalid", _codes(ergebnis))
                self.assertFalse(ergebnis["eligible"])


class TestContract(unittest.TestCase):
    def test_ok_requires_every_proof_at_once(self):
        ergebnis = _assess(snapshot_id="abc123")
        self.assertEqual(ergebnis["status"], STATUS_OK)
        self.assertTrue(ergebnis["eligible"])
        self.assertEqual(ergebnis["reasons"], [])
        self.assertEqual(ergebnis["schema_version"], SCHEMA_VERSION)
        self.assertEqual(ergebnis["snapshot"]["id"], "abc123")

    def test_the_result_is_json_ready(self):
        text = json.dumps(_assess())
        self.assertIn('"eligible": true', text)
        self.assertIn('"status": "ok"', text)

    def test_reason_codes_are_stable_and_carry_text(self):
        ergebnis = assess_source_quality(
            source_timestamp="2026-10-24 14:30:00", now=NOW,
            max_age_seconds=LIMIT, multiplier=None,
        )
        for grund in ergebnis["reasons"]:
            self.assertRegex(grund["code"], r"^[a-z0-9_]+$")
            self.assertTrue(grund["message"])
            self.assertNotEqual(grund["message"], grund["code"])


class TestSnapshotIdentity(unittest.TestCase):
    def test_the_same_snapshot_gets_the_same_id(self):
        chain = make()
        a = snapshot_identity("SPY", "2026-10-16", chain, "2026-10-07 14:30")
        b = snapshot_identity("SPY", "2026-10-16", chain, "2026-10-07 14:30")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 16)

    def test_a_different_fetch_gets_a_different_id(self):
        chain = make()
        a = snapshot_identity("SPY", "2026-10-16", chain, "2026-10-07 14:30")
        b = snapshot_identity("SPY", "2026-10-16", chain, "2026-10-07 15:30")
        self.assertNotEqual(a, b)
        c = snapshot_identity("QQQ", "2026-10-16", chain, "2026-10-07 14:30")
        self.assertNotEqual(a, c)

    def test_an_empty_chain_still_gets_an_id(self):
        self.assertEqual(len(snapshot_identity("SPY", None, [], None)), 16)


class TestTheGapIsNowNamed(unittest.TestCase):
    """The regression test for the documented hole.

    ``filter_stale`` still reads a naive stamp as UTC - that is deliberate, it is
    the manual analysis path and its behaviour may not change. This test pins the
    difference so nobody mistakes it for a quality proof again: the same input
    that the screener keeps is refused by the gate.
    """

    def test_the_screener_keeps_what_the_gate_refuses(self):
        from paperbroker.screener import ScreenerCriteria, filter_stale

        kette = [quote("SPY261218C00500000", quote_timestamp="2026-10-24 14:30:00")]
        behalten, verworfen = filter_stale(
            kette, ScreenerCriteria(max_quote_age_min=30),
            now_fn=lambda: arrow.get("2026-10-24T14:35:00+00:00"),
        )
        # The manual path keeps it (naive == UTC), unchanged by this phase.
        self.assertEqual(len(behalten), 1)
        self.assertEqual(verworfen, [])

        # The automated gate must not follow that assumption.
        eva = assess_source_quality(
            source_timestamp=kette[0].quote_timestamp,
            source_timezone=None,
            now=NOW,
            max_age_seconds=LIMIT,
            multiplier=None,
        )
        self.assertEqual(eva["status"], STATUS_UNKNOWN)
        self.assertFalse(eva["eligible"])
        self.assertIn("source_timezone_unproven", _codes(eva))


class TestRoute(unittest.TestCase):
    """The route is what a caller reads - the gate has to arrive there."""

    def setUp(self):
        self.app = create_app(quote_adapter=_MockAdapter(make()), screener=None,
                              now_fn=_clock())
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def _screen(self, **extra):
        parameter = {"ticker": "SPY", "expiration": "2026-10-16"}
        parameter.update(extra)
        antwort = self.client.get("/api/screen", query_string=parameter)
        self.assertEqual(antwort.status_code, 200)
        return antwort.get_json()

    def test_the_route_reports_unknown_quality_for_cboe_data(self):
        koerper = self._screen()
        eva = koerper["eva_quality"]
        self.assertEqual(eva["status"], STATUS_UNKNOWN)
        self.assertFalse(eva["eligible"])
        self.assertIn("source_timezone_unproven", [g["code"] for g in eva["reasons"]])
        self.assertIn("contract_multiplier_unknown", [g["code"] for g in eva["reasons"]])

    def test_every_vertical_carries_the_same_snapshot_id_and_no_approval(self):
        koerper = self._screen()
        kennung = koerper["eva_quality"]["snapshot"]["id"]
        self.assertTrue(kennung)
        for eintrag in koerper["verticals"]:
            self.assertEqual(eintrag["snapshot_id"], kennung)
            self.assertFalse(eintrag["eva_eligible"])

    def test_ignore_quote_age_never_produces_an_approval(self):
        koerper = self._screen(ignore_quote_age="1")
        eva = koerper["eva_quality"]
        self.assertEqual(eva["status"], STATUS_DISABLED)
        self.assertFalse(eva["eligible"])
        self.assertEqual(koerper["freshness_limit"], None)

    def test_the_existing_fields_are_unchanged(self):
        koerper = self._screen()
        for feld in ("ticker", "expiration", "chain_size", "candidates", "dropped",
                     "dropped_reasons", "criteria", "freshness", "verticals",
                     "vertical_drops", "results", "trend"):
            self.assertIn(feld, koerper)
        self.assertIsInstance(koerper["verticals"], list)

    def test_the_picker_stage_has_the_key_but_no_snapshot(self):
        koerper = self._screen(expiration="")
        self.assertIn("eva_quality", koerper)
        self.assertIsNone(koerper["eva_quality"])


if __name__ == "__main__":
    unittest.main()
