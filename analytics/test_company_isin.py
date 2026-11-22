"""The ISIN comes from Wikidata, and only when its country prefix matches the company's country.

Yahoo holds no ISIN: `info["isin"]` is null for every paper measured, and `Ticker.isin` answers with a
foreign paper's number (FR0011149590 for L'Oreal instead of FR0000120321, AR0725224551 for ASML
instead of NL0010273215). An invented identifier is worse than none, so the value is verified.
"""

import pathlib
import tempfile
import unittest
from email.message import Message
from unittest import mock
from urllib.error import HTTPError

import app
import store


def answers(*values):
    """Wikidata for any number of terms: every search finds an entity, every entity offers these ISINs."""
    def answer(params):
        if params.get("action") == "wbsearchentities":
            return {"search": [{"id": "Q1", "label": "Beispiel AG"}]}
        return {"entities": {"Q1": {"claims": {"P946": [
            {"mainsnak": {"datavalue": {"value": value}}} for value in values]}}}}
    return answer


class CompanyIsinTests(unittest.TestCase):
    def setUp(self):
        self.db = str(pathlib.Path(tempfile.mkdtemp()) / "isin.db")

    def test_a_matching_isin_is_taken(self):
        with mock.patch.object(app, "_wikidata", side_effect=answers("FR0000120321")):
            self.assertEqual("FR0000120321", app.company_isin("L'Oréal S.A.", "France", self.db))

    def test_a_foreign_prefix_is_refused(self):
        """Two letters must agree - a name match alone is not evidence."""
        with mock.patch.object(app, "_wikidata", side_effect=answers("DE0007164600")):
            self.assertEqual("", app.company_isin("L'Oréal S.A.", "France", self.db))

    def test_a_malformed_value_is_refused(self):
        for value in ("FR000012032", "FR00001203211", "fr0000120321", "", "FR000012032X", 12345):
            with mock.patch.object(app, "_wikidata", side_effect=answers(value)):
                self.assertEqual("", app.company_isin("L'Oréal S.A.", "France", self.db), value)

    def test_an_unknown_country_is_not_guessed(self):
        with mock.patch.object(app, "_wikidata") as look:
            self.assertEqual("", app.company_isin("L'Oréal S.A.", "Narnia", self.db))
        look.assert_not_called()

    def test_a_silent_service_answers_empty_and_is_not_stored(self):
        with mock.patch.object(app, "_wikidata", return_value=None):
            self.assertEqual("", app.company_isin("L'Oréal S.A.", "France", self.db))
        with store.connect(self.db) as conn:
            self.assertIsNone(store.read_company_isin(conn, app.company_key("L'Oréal S.A.")))

    def test_a_verified_isin_is_never_asked_for_again(self):
        with mock.patch.object(app, "_wikidata", side_effect=answers("FR0000120321")):
            app.company_isin("L'Oréal S.A.", "France", self.db)
        with mock.patch.object(app, "_wikidata") as look:
            self.assertEqual("FR0000120321", app.company_isin("L'Oréal S.A.", "France", self.db))
        look.assert_not_called()

    def test_the_legal_form_is_peeled_off_for_the_search(self):
        """"L'Oréal S.A." finds nothing at Wikidata, "L'Oréal" finds the company."""
        self.assertEqual(["L'Oréal S.A.", "L'Oréal"], app.isin_search_terms("L'Oréal S.A."))
        self.assertEqual(["SAP SE", "SAP"], app.isin_search_terms("SAP SE"))
        self.assertEqual(["LVMH"], app.isin_search_terms("LVMH"))

    def test_a_rate_limit_is_respected(self):
        """Wikidata answers 429 when asked too often; then nothing is asked for five minutes."""
        app._wikidata_blocked_until = 0.0
        refusal = HTTPError("https://www.wikidata.org/w/api.php", 429, "too many", Message(), None)
        with mock.patch.object(app, "urlopen", side_effect=refusal):
            self.assertIsNone(app._wikidata({"action": "wbsearchentities"}))
        self.assertGreater(app._wikidata_blocked_until, 0, "a refusal sets the pause")
        with mock.patch.object(app, "urlopen") as ask:
            self.assertIsNone(app._wikidata({"action": "wbsearchentities"}))
        ask.assert_not_called()
        app._wikidata_blocked_until = 0.0

    def test_another_status_does_not_pause_the_service(self):
        app._wikidata_blocked_until = 0.0
        refusal = HTTPError("https://www.wikidata.org/w/api.php", 503, "busy", Message(), None)
        with mock.patch.object(app, "urlopen", side_effect=refusal):
            self.assertIsNone(app._wikidata({"action": "wbsearchentities"}))
        self.assertEqual(0.0, app._wikidata_blocked_until)


if __name__ == "__main__":
    unittest.main()
