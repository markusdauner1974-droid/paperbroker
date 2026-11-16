"""Der Rueckfall-Test: was passiert, wenn ein Schlüssel im Zielbuch fehlt.

Die drei Einzelfaelle stehen schon in ``FallbackTest`` (Zielbuch gewinnt, sonst
Quellbuch, sonst der Schluessel selbst). Was dort NICHT steht, ist die Frage, die
im Betrieb zaehlt:

    Kann irgendein Schluessel des AUSGELIEFERTEN Buches je eine leere Zeile
    ergeben, oder eine Zeile mit einem unausgefuellten Platzhalter?

Das ist die teure Panne - nicht der Absturz, sondern die stille Leerstelle. Der
Test laeuft deshalb ueber das ECHTE Buch, nicht ueber bestueckte Fixtures, und
ueber JEDE registrierte Sprache.
"""

from __future__ import annotations

import unittest

from paperbroker import texts
from paperbroker.texts import registry


def _werte(code: str) -> dict[str, object]:
    return dict(texts.book(code))


class TheShippedBookHasNoHoleTest(unittest.TestCase):
    """Kein Wert im ausgelieferten Buch ist leer - auch nicht "nur Leerzeichen"."""

    def test_no_shipped_value_is_blank(self):
        for code in texts.languages():
            for schluessel, wert in _werte(code).items():
                roh = list(wert.values()) if isinstance(wert, dict) else [wert]
                for satz in roh:
                    with self.subTest(sprache=code, schluessel=schluessel):
                        self.assertIsInstance(satz, str)
                        self.assertTrue(str(satz).strip(), f"{code}/{schluessel} ist leer")


class EveryShippedKeyReadsInEveryLanguageTest(unittest.TestCase):
    """Jeder Schluessel des Quellbuchs liefert in jeder Sprache etwas Sichtbares.

    Zwei Zusagen auf einmal:

    * **Nie der Schluessel selbst**, solange das Quellbuch ihn kennt - der
      Rueckfall greift, bevor der Schluessel durchschlaegt.
    * **Nie leer**, egal ob uebersetzt oder zurueckgefallen.
    """

    def test_every_key_reads_somewhere(self):
        quelle = _werte(registry.DEFAULT_LANGUAGE)
        for code in texts.languages():
            ziel = _werte(code)
            for schluessel, wert in quelle.items():
                if isinstance(wert, dict):
                    continue  # Zweige deckt die Plural-Matrix ab
                with self.subTest(sprache=code, schluessel=schluessel):
                    satz = texts.t(schluessel, language=code)
                    self.assertTrue(satz.strip(), f"{code}/{schluessel} ist leer")
                    self.assertNotEqual(
                        satz, schluessel,
                        f"{code}/{schluessel} faellt auf den Schluessel durch, "
                        "obwohl das Quellbuch ihn kennt")
                    if schluessel not in ziel:
                        self.assertEqual(
                            satz, wert,
                            f"{code}/{schluessel} ist nicht der deutsche Satz - "
                            "der Rueckfall greift nicht")


class AnUnknownSectionFallsBackAsAWholeTest(unittest.TestCase):
    """Ein nicht deklarierter Abschnitt ist GANZ deutsch, nicht halb.

    Das ist die Regel, die den Alt-Endpunkten null Code kostet: ``scan``,
    ``exclusion``, ``earnings`` und ``page_route`` stehen nur im deutschen Buch,
    und das Zielbuch sagt mit einer leeren ``SECTIONS``-Liste, dass es sie nicht
    uebersetzt.
    """

    def test_every_section_is_either_fully_declared_or_fully_absent(self):
        for code in texts.languages():
            if code == registry.DEFAULT_LANGUAGE:
                continue
            buch = _werte(code)
            deklariert = set(getattr(texts._BOOKS[code], "SECTIONS", ()))
            for schluessel in buch:
                with self.subTest(sprache=code, schluessel=schluessel):
                    self.assertIn(schluessel.split(".", 1)[0], deklariert,
                                  f"{code}/{schluessel} steht im Buch, aber sein "
                                  "Abschnitt ist nicht deklariert")

    def test_a_section_absent_from_the_target_book_reads_german(self):
        de = _werte(registry.DEFAULT_LANGUAGE)
        for code in texts.languages():
            if code == registry.DEFAULT_LANGUAGE:
                continue
            ziel = _werte(code)
            fehlend = [k for k in de if k not in ziel and not isinstance(de[k], dict)]
            for schluessel in fehlend:
                with self.subTest(sprache=code, schluessel=schluessel):
                    self.assertEqual(texts.t(schluessel, language=code), de[schluessel])


if __name__ == "__main__":
    unittest.main()
