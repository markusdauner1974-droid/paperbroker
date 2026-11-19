"""Die Aktienanalyse als eingebetteter Baustein: ``/qualitaet/*``.

Der Baustein ist ein eigener Dienst (``aktienanalyse/``, eigener Prozess, Port
8766) und kennt seinen Pfadanfang selbst (``--base-path /qualitaet``). Diese
Route schickt die Anfrage **unveraendert** dorthin - Pfad mit Praefix, Abfrage
und Anfrageteil. Sie schreibt nichts um: keine Kopf-Injektion, kein neues
HTML, kein Umschreiben des Inhalts. Was der Baustein sagt, sagt sie weiter.

Warum eine Weiterleitung und kein zweiter Bau im Dashboard: der Baustein bleibt
eigenstaendig lauffaehig (Port 8766, ohne ``--base-path`` genau wie heute), und
das Dashboard muss seine Analyse nicht kennen - es kennt nur eine Adresse.

Ausfall: Ist der Baustein nicht erreichbar, antwortet diese Route 503 mit einem
lesbaren Satz in der Sprache der Anfrage und schreibt eine Zeile ins Protokoll.
Der Rest des Dashboards bleibt davon unberuehrt - die Weiterleitung ist eine
Route unter vielen, kein Vorschalten vor alle.
"""

from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request

from flask import Response, request

from ..texts import t

logger = logging.getLogger(__name__)

BACKEND_ENV = "AKTIENANALYSE_BACKEND_URL"
DEFAULT_BACKEND = "http://aktienanalyse:8766"
TIMEOUT_ENV = "AKTIENANALYSE_TIMEOUT_S"
DEFAULT_TIMEOUT = 180.0  # SEK-Abrufe dauern 10-15 s; ein Zeitlimit darunter waere falsch

# Nur diese Koepfe gehen weiter. Ein Durchreichen aller Koepfe wuerde die
# Hop-by-hop-Regeln verletzen (Connection, Transfer-Encoding) und dem Baustein
# Koepfe unterschieben, die er nicht gesetzt hat.
PASS_HEADERS = ("content-type", "content-length", "cache-control")

# Groesse der Stuecke, in denen der Koerper in beide Richtungen fliesst.
CHUNK = 64 * 1024


def backend_url() -> str:
    """Die Adresse des Bausteins, ohne abschliessenden Schraegstrich."""
    return (os.environ.get(BACKEND_ENV) or DEFAULT_BACKEND).rstrip("/")


def timeout_seconds() -> float:
    try:
        wert = float(os.environ.get(TIMEOUT_ENV, ""))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return wert if wert > 0 else DEFAULT_TIMEOUT


def language() -> str:
    """Die Sprache der Anfrage, wie sie die Seite sonst auch liest."""
    return request.args.get("lang") or "de"


def unavailable(message: str) -> Response:
    """Die eine Antwort, wenn der Baustein nicht antwortet.

    Kein Stacktrace, kein leerer 500er: eine Seite mit einem Satz, damit der
    Mensch weiss, was los ist, und ein Log, damit der Betreiber es findet.
    """
    seite = (
        "<!doctype html><html lang=\"{lang}\"><meta charset=\"utf-8\">"
        "<title>{titel}</title>"
        "<style>body{{margin:0;padding:3rem;background:#091217;color:#fafafa;"
        "font:15px/1.55 system-ui,sans-serif}}h1{{font-size:1.1rem;font-weight:600}}"
        "</style><h1>{text}</h1></html>"
    ).format(lang=language(), titel=t("quality.unavailable.title", language=language()),
             text=t("quality.unavailable.text", language=language()))
    return Response(seite, status=503, content_type="text/html; charset=utf-8")


def _forwarded_url() -> str:
    # ``request.path`` traegt das Praefix bereits (/qualitaet/...), der Baustein
    # erwartet genau diesen Pfad - deshalb wird hier nichts abgeschnitten.
    query = request.query_string.decode("latin-1")
    return backend_url() + request.path + (("?" + query) if query else "")


def _relay(rest=None) -> Response:
    """Eine Anfrage an den Baustein, Antwort unveraendert zurueck.

    ``rest`` kommt von der Regel ``/qualitaet/<path:rest>`` und wird nicht
    gebraucht: weitergegeben wird immer ``request.path``, also der Pfad mit
    Praefix - so wie der Baustein ihn erwartet.
    """
    ziel = _forwarded_url()
    koerper = request.get_data() if request.method in ("POST", "PUT", "PATCH") else None
    kopf = {}
    if koerper is not None:
        kopf["Content-Type"] = request.headers.get("Content-Type", "application/octet-stream")
    anfrage = urllib.request.Request(ziel, data=koerper, method=request.method, headers=kopf)
    try:
        antwort = urllib.request.urlopen(anfrage, timeout=timeout_seconds())
    except urllib.error.HTTPError as fehler:
        # Ein Fehler des Bausteins ist eine ANTWORT, kein Ausfall: 400, 404 oder
        # 500 gehen mit Status, Kopf und Koerper weiter, als waere nichts
        # gewesen. ``HTTPError`` ist eine Unterklasse von ``URLError`` und muss
        # deshalb VOR ihr gefangen werden - sonst wuerde jede Fehlerseite des
        # Bausteins zu "nicht erreichbar".
        antwort = fehler
    except (urllib.error.URLError, OSError, ValueError) as fehler:
        logger.warning("%s nicht erreichbar (%s): %s", BACKEND_ENV, ziel, fehler)
        return unavailable(str(fehler))

    weiter = {name: antwort.headers.get(name) for name in PASS_HEADERS
              if antwort.headers.get(name)}

    def stuecke():
        # Der Koerper wird stueckweise weitergegeben und hier geschlossen: bei
        # einer 730-KB-Antwort eines Tickers soll nichts zwischengespeichert
        # werden, was niemand braucht.
        try:
            while True:
                stueck = antwort.read(CHUNK)
                if not stueck:
                    break
                yield stueck
        finally:
            antwort.close()

    return Response(stuecke(), status=antwort.status, headers=weiter)


def register(app) -> None:
    """Zwei Regeln: die Wurzel des Bausteins mit und ohne Schraegstrich."""
    app.add_url_rule("/qualitaet/", endpoint="quality_root", view_func=_relay,
                     methods=["GET", "POST"], strict_slashes=False)
    app.add_url_rule("/qualitaet/<path:rest>", endpoint="quality_any", view_func=_relay,
                     methods=["GET", "POST"])
