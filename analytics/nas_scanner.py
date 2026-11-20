"""Read the NAS scanner's published stock snapshots."""

import json
import os
import sys
from getpass import getpass
from http.cookiejar import CookieJar
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

SECRET_FILE = "/run/secrets/nas_password"
PASSWORD_ENV = "AKTIENANALYSE_NAS_PASSWORD"
# The scanner's address: in manual operation the known one in the home network, in production
# through AKTIENANALYSE_NAS_URL. Both are the same string, kept in one place.
DEFAULT_ORIGIN = "http://192.168.178.10:8089"


def configured_password():
    """The password for unattended operation, or ``None``.

    Order: the mounted file first, then the environment variable. Both ways are
    meant for containers where nobody can type; entering it at the terminal stays
    the third way (see ``connect``).

    The value is NOT logged, not printed and not put into an error message - a
    password in a log line is a password written down.
    """
    try:
        with open(SECRET_FILE, encoding="utf-8") as handle:
            value = handle.read().strip()
    except OSError:
        value = ""
    if not value:
        value = (os.environ.get(PASSWORD_ENV) or "").strip()
    return value or None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class NasScanner:
    def __init__(self, origin):
        url = urlsplit(origin)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise ValueError("Die NAS-Adresse muss aus Protokoll, Host und optionalem Port bestehen.")
        self.origin = origin.rstrip("/")
        self.opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPCookieProcessor(CookieJar()))
        self.lock = Lock()

    def request_json(self, path, body=None, *, expected=dict):
        data = json.dumps(body).encode() if body is not None else None
        req = Request(self.origin + "/api/v1/" + path, data=data,
                      headers={"Content-Type": "application/json"} if data else {})
        # The lock guards the opener and its cookie jar, not the body: holding it
        # across the read would queue every scanner request behind the slowest
        # response in flight.
        with self.lock:
            opened = self.opener.open(req, timeout=8)
        with opened as response:
            raw = response.read(3_000_001)
        if len(raw) > 3_000_000:
            raise ValueError("Die NAS-Antwort ist zu groß.")
        result = json.loads(raw)
        if not isinstance(result, expected):
            raise ValueError("Unerwartetes Antwortformat vom NAS.")
        return result

    def search(self, query):
        return self.request_json("stocks/search?" + urlencode({"q": query, "limit": 20}), expected=list)

    def histories(self, symbols):
        try:
            return self.batch("history", {"symbols": symbols, "period": "5y"})
        except HTTPError as error:
            if error.code not in (404, 405):
                raise
            error.close()
        data = {}
        for symbol in symbols:
            try:
                data[symbol] = self.request_json(
                    "stocks/" + quote(symbol, safe="") + "/history?period=5y", expected=list)
            except HTTPError as error:
                if error.code != 404:
                    raise
                error.close()
        return data

    def fundamentals(self, symbols):
        try:
            return self.batch("fundamentals", {"symbols": symbols})
        except HTTPError as error:
            if error.code not in (404, 405):
                raise
            error.close()
        data = {}
        for symbol in symbols:
            try:
                dashboard = self.request_json("stocks/" + quote(symbol, safe="") + "/decision-dashboard")
            except HTTPError as error:
                if error.code != 404:
                    raise
                error.close()
                continue
            raw = dashboard.get("fundamentals")
            if dashboard.get("symbol") == symbol and isinstance(raw, dict):
                data[symbol] = raw
        return data

    def batch(self, endpoint, body):
        response = self.request_json("stocks/" + endpoint + "/batch", body)
        data = response.get("data")
        if not isinstance(data, dict):
            raise ValueError("Der NAS-Sammelabruf enthält keine gültige Datenliste.")
        return data

    def connect(self):
        status = self.request_json("auth/status")
        if not status.get("required") or status.get("authenticated"):
            return
        if not status.get("configured"):
            raise ValueError("Die NAS-Anmeldung ist noch nicht eingerichtet.")
        # The mounted file first, then the environment variable, the keyboard last: in a
        # container there is nobody who could type.
        password = configured_password()
        if password is None:
            if not sys.stdin or not sys.stdin.isatty():
                raise ValueError("NAS-Anmeldung ohne Passwort-Quelle: weder "
                                 + SECRET_FILE + " noch " + PASSWORD_ENV + " gesetzt.")
            password = getpass("Stock-Scanner-Passwort für " + self.origin + " (Eingabe verborgen): ")
        if not password:
            raise ValueError("NAS-Anmeldung übersprungen.")
        result = self.request_json("auth/login", {"password": password})
        if not result.get("authenticated"):
            raise ValueError("NAS-Anmeldung fehlgeschlagen.")

    def industry_group(self, chart):
        name = chart.get("ibd_industry_group")
        market = chart.get("market")
        if not isinstance(name, str) or not name.strip() or not isinstance(market, str) or not market:
            return None
        result = {"name": name, "market": market, "rank": chart.get("ibd_group_rank"), "symbols": []}
        query = urlencode({"group": name, "market": market, "days": 14})
        try:
            detail = self.request_json("groups/rankings/detail?" + query)
        except (URLError, OSError, ValueError):
            return result
        if detail.get("industry_group") != name:
            return result
        result.update({
            "rank": detail.get("current_rank", result["rank"]),
            "count": detail.get("num_stocks"),
            "symbols": list(dict.fromkeys(item["symbol"] for item in (detail.get("stocks") or [])
                                         if isinstance(item, dict) and isinstance(item.get("symbol"), str))),
        })
        return result

    def snapshot(self, symbol):
        try:
            payload = self.request_json("stocks/" + quote(symbol, safe="") + "/decision-dashboard")
        except HTTPError as error:
            if error.code in (401, 403):
                return {"status": "signed_out", "message": "NAS-Sitzung abgelaufen. Aktienanalyse neu starten und erneut anmelden."}
            if error.code == 404:
                return {"status": "missing",
                        "message": "Für diesen Ticker fehlt ein NAS-Snapshot oder die NAS-Version "
                                   "unterstützt diese API nicht."}
            raise
        if payload.get("symbol") != symbol:
            raise ValueError("Der Snapshot gehört zu einem anderen Ticker.")

        def pick(section, *keys):
            if not isinstance(section, dict):
                return {}
            return {key: section[key] for key in keys if key in section}

        chart = (payload.get("chart") or {}).get("chart_data") or {}
        group = self.industry_group(chart)
        return {
            "status": "ready",
            "industry_group": group,
            "symbol": symbol,
            "as_of_date": payload.get("as_of_date"),
            "degraded": payload.get("degraded_reasons") or [],
            "fundamentals": pick(payload.get("fundamentals"), "pe_ratio", "peg_ratio",
                                 "eps_current", "profit_margin", "field_provenance",
                                 "institutional_ownership"),
            "technicals": pick(payload.get("technicals"), "current_price", "ma_50",
                               "ma_150", "ma_200", "high_52w", "low_52w"),
            "scan": pick(chart, "scan_date", "rs_rating", "rs_rating_1m", "rs_rating_3m",
                         "rs_rating_12m", "eps_rating", "stage", "adr_percent",
                         "minervini_score", "canslim_score", "vcp_detected", "vcp_pivot"),
            "decision": pick(payload.get("decision_summary"), "composite_score", "composite_method",
                             "screeners_passed", "screeners_total"),
            "events": pick(payload.get("event_risk"), "next_earnings_date", "beat_count_last_4",
                           "miss_count_last_4", "recent_earnings_count"),
            "peers": [item["symbol"] for item in (payload.get("peers") or [])[:8]
                      if isinstance(item, dict) and isinstance(item.get("symbol"), str)],
            "currency": ((payload.get("fundamentals") or {}).get("fx_metadata") or {}).get("from_currency"),
        }
