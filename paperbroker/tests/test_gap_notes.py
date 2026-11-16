"""Die Methodik-Notiz muss die Schranke nennen, die die Antwort angewandt hat.

Die Route nimmt ``?max_required_move_pct=`` pro Anfrage an, akzeptiert Werte in
(0, 100] und schreibt die Schranke zweimal in den Rumpf. Die Notiz darueber nennt
sie ebenfalls - und zwar ohne Rundung: ``f"{wert:.0f}"`` machte aus 0.4 eine
"0" und aus 2.5 eine "2" (half-to-even), waehrend der Rumpf derselben Antwort 0.4
und 2.5 auswies. Wer beides liest, sieht einen Widerspruch.

Geprueft wird deshalb nicht die Formatierung, sondern die Gleichheit von Notiz
und Rumpf - genau das, was ein Leser vergleicht.
"""

from __future__ import annotations

import re
import unittest

from .test_gap_funnel import DAY
from .test_gap_spreads_detail_endpoint import _client

_NOTE_ZAHL = re.compile(r"bis\s*([0-9.eE+-]+)\s*%")


class TheNoteNamesTheAppliedBoundTest(unittest.TestCase):
    """Notiz und Rumpf nennen dieselbe Zahl - auch bei krummen Schranken."""

    def _notiz_zahl(self, client, schranke: str):
        antwort = client.get(
            f"/api/v1/gap-spreads?day={DAY}&max_required_move_pct={schranke}"
        )
        self.assertEqual(antwort.status_code, 200)
        rumpf = antwort.get_json()
        notizen = [n for n in rumpf["notes"] if "zugelassen" in n]
        self.assertEqual(len(notizen), 1, "genau eine Notiz nennt die Schranke")
        treffer = _NOTE_ZAHL.search(notizen[0])
        if treffer is None:
            self.fail(f"keine Zahl in: {notizen[0]!r}")
        return rumpf["max_required_move_pct"], treffer.group(1)

    def test_the_note_shows_the_same_number_as_the_body(self):
        client = _client()
        for schranke in ("0.4", "2.5", "12.345678", "30", "100"):
            with self.subTest(schranke=schranke):
                rumpf_wert, notiz_wert = self._notiz_zahl(client, schranke)
                self.assertEqual(float(notiz_wert), float(rumpf_wert),
                                 f"Notiz {notiz_wert!r} != Rumpf {rumpf_wert!r}")

    def test_the_note_does_not_round_a_fraction_away(self):
        """Der Fall, der den Fehler sichtbar machte: 0.4 wurde zu "0"."""
        client = _client()
        _, notiz_wert = self._notiz_zahl(client, "0.4")
        self.assertEqual(notiz_wert, "0.4")

    def test_the_note_drops_a_pointless_trailing_zero(self):
        """30.0 heisst in der Notiz "30" - die Zahl, nicht die Schreibweise."""
        client = _client()
        _, notiz_wert = self._notiz_zahl(client, "30")
        self.assertEqual(notiz_wert, "30")


if __name__ == "__main__":
    unittest.main()
