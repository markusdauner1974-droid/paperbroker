"""(f2) Die Sprach-Merkung - nur im Browser, nie auf dem Server.

Der Server bleibt zustandslos: kein Cookie, keine Sitzung, kein
``Accept-Language``. Was hier passiert, ist eine einzige Regel, die der Browser
sich merkt:

* ``?lang=x`` mit gueltigem Kuerzel: **explizit gewinnt** - die Merkung wird auf
  ``x`` gesetzt, es wird nicht weitergespielt.
* nackter Aufruf (kein ``lang``): traegt die Merkung ein gueltiges Kuerzel, das
  sich von der aufgeloesten Sprache unterscheidet, wird auf dieselbe Adresse mit
  ``?lang=<gemerkt>`` weitergeleitet; alle uebrigen Parameter bleiben.
* ungueltige oder entfernte Merkung: Schluessel loeschen, normal weiter. Ein
  gespeicherter Wert wird NIE ungeprueft in eine Adresse eingesetzt; die
  Weiterleitungs-Adresse entsteht aus der AKTUELLEN Adresse.

Die Anweisung steht INLINE im ``<head>`` und laeuft damit, bevor der Koerper
gezeichnet wird - die deutsche Seite blitzt bei einem gemerkten ``en`` nicht auf.
Ihre einzige Datenquelle ist das JSON-Blatt direkt darueber, das der Server aus
der Registry rendert; der Injektions-Block im Koerper bleibt unberuehrt (er wird
erst spaeter gelesen, nicht von hier).

Der Text liegt als Konstante hier, und der Test liest ihn WOERTLICH aus der
gerenderten Seite: geprueft wird, was ausgeliefert wird.
"""

from __future__ import annotations

#: Name des Browser-Schluessels. Ein Praefix, damit die Seite nicht in fremden
#: Speichern wuehlt.
STORAGE_KEY = "paperbroker.lang"

#: Das ``id`` des JSON-Datenblatts im Kopf.
BOOT_ID = "lang-boot"

#: Das ``id`` der Anweisung selbst (der Test findet sie damit eindeutig).
SCRIPT_ID = "lang-memo"

#: Die Anweisung. Bewusst ES5-nah, ohne Baukette und ohne ``<`` im Text: sie
#: steht im Kopf jeder Seite und muss vor dem ersten Pixel laufen. Die zwei
#: Namen werden unten eingesetzt, damit der Text EIN Stueck bleibt.
REMEMBER_SCRIPT = """(function () {
  'use strict';
  var boot = JSON.parse(document.getElementById('__BOOT__').textContent);
  var store = window.localStorage;
  var key = '__KEY__';
  var asked = new URLSearchParams(window.location.search).get('lang');
  if (asked !== null) {
    // Genau die Normalisierung des Servers (resolve_language: strip + lower),
    // sonst rendert '?lang=EN' englisch, waehrend die Merkung nichts schreibt -
    // und der naechste nackte Aufruf springt auf die alte Sprache zurueck.
    asked = asked.trim().toLowerCase();
    if (asked === '') { asked = null; }
  }
  if (asked !== null) {
    if (boot.allowed.indexOf(asked) !== -1) { store.setItem(key, asked); }
    return;
  }
  var remembered = store.getItem(key);
  if (remembered === null) { return; }
  if (boot.allowed.indexOf(remembered) === -1) { store.removeItem(key); return; }
  if (remembered === boot.resolved) { return; }
  var target = new URL(window.location.href);
  target.searchParams.set('lang', remembered);
  window.location.replace(target.toString());
})();"""

REMEMBER_SCRIPT = REMEMBER_SCRIPT.replace("__BOOT__", BOOT_ID).replace("__KEY__", STORAGE_KEY)
