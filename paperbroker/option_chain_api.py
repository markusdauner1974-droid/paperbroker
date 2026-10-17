"""Serializer for the unfiltered option-chain endpoint (GET /api/v1/option-chain).

Pure functions over a raw CBOE snapshot. The snapshot handed in is never
modified: every row is read, classified and copied into the response schema.
Nothing here screens, scores or decides - the caller does that, and this module
deliberately gives it the data to decide WITH rather than a filter it cannot see.

Design rules, each one a measured property of the feed (2026-10-17):

- The source values are already decimal: ``iv`` 0.25 means 25 %, ``delta``
  0.40 means 0.4. The adapter's ``OptionQuote`` multiplies both by 100, so this
  module reads the raw rows and never touches an ``OptionQuote``.
- A missing value stays ``None``; a real ``0`` stays ``0``. ``None`` never
  becomes 0, and a last trade is never replaced by the bid/ask midpoint.
- Neither the envelope timestamp (``2026-10-17 03:33:37``) nor
  ``last_trade_time`` (``2026-10-16T11:40:42``) carries a timezone, so no UTC
  value and no quote age can be derived. The raw string is preserved and
  ``SOURCE_TIMEZONE_UNKNOWN`` is reported; the server's own timezone is never
  silently assumed.
- The source names no multiplier, deliverable, exercise style, settlement type
  or currency, so all of them stay ``None`` with
  ``CONTRACT_METADATA_UNKNOWN``. 100 is not inferred from the option symbol.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timezone
from typing import Any

SCHEMA_VERSION = "1.0"

# OCC option symbol: root, then YYMMDD, then C/P, then the strike in
# thousandths on eight digits (AAPL261016C00130000 -> 130.0). Roots may carry
# digits (BRK.B style feeds use letters plus a digit), hence [A-Z0-9]+.
_OCC_RE = re.compile(r"^(?P<root>[A-Z0-9]{1,6})(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")

SOURCE_TIMEZONE_UNKNOWN = "SOURCE_TIMEZONE_UNKNOWN"
CONTRACT_METADATA_UNKNOWN = "CONTRACT_METADATA_UNKNOWN"
UNSUPPORTED_SYMBOL = "UNSUPPORTED_SYMBOL"
NONFINITE_VALUE = "NONFINITE_VALUE"
DUPLICATE_SOURCE_SYMBOL = "DUPLICATE_SOURCE_SYMBOL"
LAST_TRADE_TIME_NO_TIMEZONE = "LAST_TRADE_TIME_NO_TIMEZONE"
OUTSIDE_REQUESTED_EXPIRATION = "OUTSIDE_REQUESTED_EXPIRATION"

# One flag per source field: a missing value and a zero value are different
# facts and must stay distinguishable downstream.
_FIELD_FLAGS = {
    "bid": ("BID_MISSING", "BID_ZERO"),
    "ask": ("ASK_MISSING", "ASK_ZERO"),
    "bid_size": ("BID_SIZE_MISSING", None),
    "ask_size": ("ASK_SIZE_MISSING", None),
    "iv": ("IV_MISSING", None),
}


def _finite(value: Any) -> Any:
    """Return a JSON-safe value: non-finite floats and NaN become None."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _json_safe(row: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Copy a source row, replacing non-finite numbers with None.

    Returns (row, had_nonfinite). ``json.dumps`` would emit ``NaN``/``Infinity``
    for these, which RFC 8259 readers reject - the body has to stay parseable.
    """
    sauber: dict[str, Any] = {}
    hatte = False
    for schluessel, wert in row.items():
        sicher = _finite(wert)
        if sicher is None and wert is not None:
            hatte = True
        sauber[schluessel] = sicher
    return sauber, hatte


def _number(row: dict[str, Any], feld: str) -> tuple[float | None, bool]:
    """Read one numeric column. Returns (value_or_None, was_nonfinite).

    Missing and non-numeric stay None; a real 0 stays 0; a non-finite value is
    reported so the contract can carry NONFINITE_VALUE.
    """
    wert = row.get(feld)
    if wert is None:
        return None, False
    if isinstance(wert, bool):
        return None, False
    try:
        zahl = float(wert)
    except (TypeError, ValueError):
        return None, False
    if not math.isfinite(zahl):
        return None, True
    return zahl, False


def _integer(row: dict[str, Any], feld: str) -> tuple[int | None, bool]:
    zahl, nichtendlich = _number(row, feld)
    if zahl is None:
        return None, nichtendlich
    return int(zahl), False


def _parse_occ_symbol(symbol: Any) -> dict[str, Any] | None:
    """Classify an option symbol. Returns None when the format is not OCC.

    Nothing is guessed from an unrecognised symbol: no call/put letter, no
    strike, no expiry. The row keeps its status instead of disappearing.
    """
    if not isinstance(symbol, str):
        return None
    treffer = _OCC_RE.match(symbol.strip().upper())
    if treffer is None:
        return None
    ymd = treffer.group("ymd")
    try:
        verfall = datetime.strptime(ymd, "%y%m%d").date()
    except ValueError:
        return None
    return {
        "underlying_symbol": treffer.group("root"),
        "expiration": verfall.isoformat(),
        "option_type": "call" if treffer.group("cp") == "C" else "put",
        "strike": int(treffer.group("strike")) / 1000.0,
    }


def _quote_flags(kontrakt: dict[str, Any], bid: float | None, ask: float | None) -> list[str]:
    flags: list[str] = []
    for feld, (fehlt, null) in _FIELD_FLAGS.items():
        wert = kontrakt.get(feld)
        if wert is None:
            flags.append(fehlt)
        elif null is not None and _number(kontrakt, feld)[0] == 0:
            flags.append(null)
    if bid is not None and bid < 0:
        flags.append("NEGATIVE_QUOTE")
    if ask is not None and ask < 0 and "NEGATIVE_QUOTE" not in flags:
        flags.append("NEGATIVE_QUOTE")
    if bid is not None and ask is not None and bid > ask:
        flags.append("CROSSED_QUOTE")
    return flags


def serialise_contract(
    row: dict[str, Any],
    *,
    requested_expiration: str | None,
    duplicate_symbols: set[str],
) -> dict[str, Any]:
    """Map one raw source row onto the version-1 contract schema."""
    flags: list[str] = []
    quelle, hatte_nichtendlich = _json_safe(row)
    if hatte_nichtendlich:
        flags.append(NONFINITE_VALUE)

    symbol = row.get("option")
    klasse = _parse_occ_symbol(symbol)
    if klasse is None:
        flags.append(UNSUPPORTED_SYMBOL)

    bid, bid_nf = _number(row, "bid")
    ask, ask_nf = _number(row, "ask")
    bid_size, bid_size_nf = _integer(row, "bid_size")
    ask_size, ask_size_nf = _integer(row, "ask_size")
    open_interest, oi_nf = _integer(row, "open_interest")
    volume, vol_nf = _integer(row, "volume")
    iv, iv_nf = _number(row, "iv")
    delta, delta_nf = _number(row, "delta")
    gamma, gamma_nf = _number(row, "gamma")
    theta, theta_nf = _number(row, "theta")
    vega, vega_nf = _number(row, "vega")
    rho, rho_nf = _number(row, "rho")
    theo, theo_nf = _number(row, "theo")
    last_trade_price, ltp_nf = _number(row, "last_trade_price")

    if any((bid_nf, ask_nf, bid_size_nf, ask_size_nf, oi_nf, vol_nf, iv_nf, delta_nf,
            gamma_nf, theta_nf, vega_nf, rho_nf, theo_nf, ltp_nf)) and NONFINITE_VALUE not in flags:
        flags.append(NONFINITE_VALUE)

    flags.extend(flag for flag in _quote_flags(row, bid, ask) if flag not in flags)

    last_trade_time_raw = row.get("last_trade_time")
    if last_trade_time_raw is not None:
        # No timezone in the feed value: UTC cannot be derived honestly.
        flags.append(LAST_TRADE_TIME_NO_TIMEZONE)

    if isinstance(symbol, str) and symbol in duplicate_symbols:
        flags.append(DUPLICATE_SOURCE_SYMBOL)
    if (
        requested_expiration is not None
        and klasse is not None
        and klasse["expiration"] != requested_expiration
        and OUTSIDE_REQUESTED_EXPIRATION not in flags
    ):
        flags.append(OUTSIDE_REQUESTED_EXPIRATION)

    return {
        "symbol": symbol,
        "underlying_symbol": klasse["underlying_symbol"] if klasse else None,
        "option_type": klasse["option_type"] if klasse else None,
        "strike": klasse["strike"] if klasse else None,
        "expiration": klasse["expiration"] if klasse else None,
        "bid": bid,
        "ask": ask,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "last_trade_price": last_trade_price,
        "last_trade_time_raw": last_trade_time_raw,
        "last_trade_time_utc": None,
        "open_interest": open_interest,
        "volume": volume,
        "iv_decimal": iv,
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
        "rho": rho,
        "theoretical_price": theo,
        "multiplier": None,
        "deliverable": None,
        "currency": None,
        "exercise_style": None,
        "settlement_type": None,
        "contract_metadata_status": "unknown",
        "parse_status": "ok" if klasse else "unsupported_symbol",
        "quality_flags": flags,
        # Everything the feed also carried, unchanged and JSON-safe.
        "open": _finite(row.get("open")),
        "high": _finite(row.get("high")),
        "low": _finite(row.get("low")),
        "change": _finite(row.get("change")),
        "percent_change": _finite(row.get("percent_change")),
        "prev_day_close": _finite(row.get("prev_day_close")),
        "tick": row.get("tick"),
        "source_record": quelle,
    }


def listed_expirations(snapshot: dict[str, Any]) -> list[str]:
    """Every expiration the snapshot reliably exposes, sorted ascending.

    Public on purpose: the route needs exactly this list to reject an
    expiration the source never offered, and reaching into the private
    symbol parser from a route would couple the two.
    """
    verfaelle: set[str] = set()
    for zeile in snapshot.get("records") or []:
        if not isinstance(zeile, dict):
            continue
        klasse = _parse_occ_symbol(zeile.get("option"))
        if klasse is not None:
            verfaelle.add(klasse["expiration"])
    return sorted(verfaelle)


def serialise_snapshot(
    snapshot: dict[str, Any],
    *,
    ticker: str,
    requested_expiration: str | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Build the version-1 response body from a raw adapter snapshot.

    Every source row is accounted for exactly once: as a returned contract, as
    a row outside the requested expiration, as an unassignable row or as an
    invalid row. The counters add up to ``source_record_count``; a counter that
    would hide a row is a bug, not a rounding difference.
    """
    rohzeilen = snapshot.get("records")
    if not isinstance(rohzeilen, list):
        rohzeilen = []
    zeilen = [z for z in rohzeilen if isinstance(z, dict)]
    nicht_dict = len(rohzeilen) - len(zeilen)

    gesehen: dict[str, int] = {}
    for zeile in zeilen:
        symbol = zeile.get("option")
        if isinstance(symbol, str):
            gesehen[symbol] = gesehen.get(symbol, 0) + 1
    doppelte = {symbol for symbol, anzahl in gesehen.items() if anzahl > 1}

    contracts: list[dict[str, Any]] = []
    unassigned: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    aussen = 0
    verfaelle: set[str] = set()

    for roh in rohzeilen:
        if isinstance(roh, dict):
            continue
        # A row that is not a mapping cannot carry a contract - but it must not
        # vanish into a counter either. It is reported with its raw value, so
        # every source row is visible in exactly one list or the other.
        sicher = roh if isinstance(roh, (str, int, float, bool)) or roh is None else str(roh)
        invalid.append(
            {
                "symbol": None,
                "parse_status": "invalid",
                "reason": "source record is not a JSON object",
                "quality_flags": [],
                "source_record": {"raw": _finite(sicher)},
            }
        )

    for zeile in zeilen:
        klasse = _parse_occ_symbol(zeile.get("option"))
        if klasse is not None:
            verfaelle.add(klasse["expiration"])

    for zeile in zeilen:
        symbol = zeile.get("option")
        klasse = _parse_occ_symbol(symbol)
        if klasse is None:
            # No call/put, strike or expiry is guessed. The row is reported
            # with its original content so a requested expiration never looks
            # verified when it is not.
            sicher, hatte_nf = _json_safe(zeile)
            eintrag = {
                "symbol": symbol,
                "parse_status": "unsupported_symbol",
                "reason": "option symbol does not match the OCC format",
                "quality_flags": [UNSUPPORTED_SYMBOL]
                + ([NONFINITE_VALUE] if hatte_nf else []),
                "source_record": sicher,
            }
            if zeile.get("option") is None or not isinstance(zeile.get("option"), str):
                invalid.append(eintrag)
            else:
                unassigned.append(eintrag)
            continue
        if requested_expiration is not None and klasse["expiration"] != requested_expiration:
            # Accounted for, but deliberately not returned: the caller asked
            # for one expiration and the count says how many rows that left out.
            aussen += 1
            continue
        contracts.append(
            serialise_contract(
                zeile,
                requested_expiration=requested_expiration,
                duplicate_symbols=doppelte,
            )
        )

    aufzeichnungen = len(rohzeilen)
    zurueck = len(contracts)
    unzuordenbar = len(unassigned)
    # Every invalid row is now materialised in `invalid` (including the rows
    # that were not mappings), so adding `nicht_dict` again would double-count.
    ungueltig = len(invalid)

    warnungen: list[str] = []
    if nicht_dict:
        warnungen.append(f"{nicht_dict} source record(s) were not JSON objects")
    if doppelte:
        warnungen.append(f"{len(doppelte)} duplicate source symbol(s) kept, not deduplicated")
    if unassigned:
        warnungen.append(f"{unzuordenbar} source record(s) could not be assigned to a contract")
    if ungueltig:
        warnungen.append(f"{ungueltig} source record(s) are invalid")
    warnungen.append(
        "source timestamp carries no timezone: source_timestamp_utc and the quote age stay null"
    )

    if unzuordenbar or ungueltig or doppelte or nicht_dict:
        vollstaendigkeit = "partial"
    elif aufzeichnungen == 0:
        vollstaendigkeit = "unknown"
    else:
        vollstaendigkeit = "complete"

    jetzt = now_fn() if now_fn is not None else datetime.now(UTC)
    if jetzt.tzinfo is None:
        jetzt = jetzt.replace(tzinfo=UTC)
    jetzt = jetzt.astimezone(UTC)

    abgerufen = snapshot.get("fetched_at")
    abgerufen_iso: str | None = None
    cache_alter: float | None = None
    if abgerufen is not None:
        try:
            abgerufen_iso = abgerufen.to("utc").isoformat() if hasattr(abgerufen, "to") else None
            if abgerufen_iso is not None:
                ab_zeit = datetime.fromisoformat(abgerufen_iso)
                cache_alter = round((jetzt - ab_zeit).total_seconds(), 3)
                if cache_alter < 0:
                    cache_alter = None
        except (TypeError, ValueError):
            abgerufen_iso = None
            cache_alter = None

    quellzeit_raw = snapshot.get("source_timestamp_raw")
    return {
        "schema_version": SCHEMA_VERSION,
        "ticker": ticker,
        "requested_expiration": requested_expiration,
        "source": {
            "name": "CBOE delayed quotes",
            "delayed": True,
            "nominal_delay_minutes": 15,
        },
        "snapshot": {
            "source_timestamp_raw": quellzeit_raw,
            "source_timestamp_utc": None,
            "source_timestamp_timezone": None,
            "fetched_at_utc": abgerufen_iso,
            "served_at_utc": jetzt.isoformat(),
            "source_age_seconds": None,
            "cache_age_seconds": cache_alter,
            "source_record_count": aufzeichnungen,
            "returned_record_count": zurueck,
            "unassigned_record_count": unzuordenbar,
            "invalid_record_count": ungueltig,
            "outside_requested_expiration_count": aussen,
            "completeness": vollstaendigkeit,
            "warnings": warnungen,
        },
        "underlying": {
            "price": _finite(snapshot.get("current_price")),
            "currency": None,
            "currency_source": None,
        },
        "listed_expirations": sorted(verfaelle),
        "contracts": contracts,
        "unassigned_records": unassigned,
        "invalid_records": invalid,
        "quality_flags": [SOURCE_TIMEZONE_UNKNOWN, CONTRACT_METADATA_UNKNOWN],
    }


def count_source_records(snapshot: dict[str, Any]) -> int:
    """Rows the source delivered, before any classification."""
    rohzeilen: Iterable[Any] = snapshot.get("records") or []
    return len(list(rohzeilen))
