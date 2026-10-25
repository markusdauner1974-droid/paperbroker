"""EVA data-quality gate for a market-data snapshot.

Pure and deterministic: no clock, no network, no cache, no I/O. Everything the
decision needs is passed in, so the same inputs always give the same answer and
every branch can be tested without a feed.

Why this exists
---------------
``paperbroker/option_chain_api.py`` states the truth about the CBOE feed: the
timestamps carry no timezone (``SOURCE_TIMEZONE_UNKNOWN``) and the contract
multiplier is not part of the payload. ``screener.filter_stale`` still reads
those same timestamps with ``arrow.get(...).to("UTC")``, which *assumes* UTC -
so a naive "2026-10-24 14:30:00" counted as five minutes old against a 14:35
clock and passed a 30-minute guard. That is an implicit assumption dressed as a
measurement.

This module refuses to make that assumption. A timestamp without a proven
timezone is ``unknown``, never ``ok``; a missing multiplier is named, never
defaulted to 100.

States
------
``ok``        every requirement proven: explicit timezone, readable timestamp,
              age inside the limit, multiplier known and positive.
``unknown``   something required is missing or unproven (no timestamp, no
              timezone, no multiplier). Fail-closed.
``stale``     readable, timezone-proven, older than the limit.
``invalid``   readable but unusable: unparseable, in the future beyond the
              clock-skew tolerance, or contradictory contract metadata.
``disabled``  the age check was switched off (``ignore_quote_age=1``). This is
              never an approval: it exists so a caller can tell "not checked"
              apart from "checked and fine".

Only ``ok`` sets ``eligible`` to True.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timezone

SCHEMA_VERSION = "1.0"

STATUS_OK = "ok"
STATUS_UNKNOWN = "unknown"
STATUS_STALE = "stale"
STATUS_INVALID = "invalid"
STATUS_DISABLED = "disabled"

# Reason codes are the machine-readable contract; the text is for humans and
# never parsed. Adding a code is additive; changing one is a contract change.
REASON_TEXT = {
    "source_timestamp_missing": "The source delivered no timestamp.",
    "source_timestamp_invalid": "The source timestamp cannot be parsed.",
    "source_timezone_unproven": "The timestamp names no timezone; it must not be read as UTC.",
    "source_timestamp_stale": "The source timestamp is older than the limit.",
    "source_timestamp_in_future": "The source timestamp lies in the future; the feed is not trustworthy.",
    "age_check_disabled": "The age check is disabled (ignore_quote_age); nothing was verified.",
    "check_clock_missing": "Without a check clock there is no age to report.",
    "fetched_at_is_not_a_source_time": "Our fetch time does not prove the freshness of the source.",
    "contract_multiplier_unknown": "The source does not document the contract multiplier.",
    "contract_multiplier_invalid": "The contract multiplier is unusable.",
}

_INVALID = frozenset({
    "source_timestamp_invalid",
    "source_timestamp_in_future",
    "check_clock_missing",
    "contract_multiplier_invalid",
})
_UNKNOWN = frozenset({
    "source_timestamp_missing",
    "source_timezone_unproven",
    "contract_multiplier_unknown",
})

# Clock skew is seconds, not minutes: a stamp a minute ahead is a slow clock, an
# hour ahead is a broken feed. Same tolerance the screener uses.
DEFAULT_FUTURE_TOLERANCE_S = 60

# Only these count as PROVEN timezone information for a naive stamp. Applying an
# IANA zone to a naive local time is ambiguous twice a year, so it is refused
# rather than guessed.
_UTC_NAMES = frozenset({"utc", "z", "etc/utc", "+00:00", "+0000", "gmt"})


def _json_scalar(value):
    """Echo a caller value without breaking json.dumps.

    A Decimal('100') is correctly rejected as a multiplier, but echoing it
    verbatim made the whole answer unserialisable - the route would answer 500
    instead of naming the problem.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _reason(code, text=None):
    return {"code": code, "message": text or REASON_TEXT.get(code, code)}


def _parse_timestamp(wert):
    """(datetime|None, aware, raw_text, error_code|None)."""
    if wert is None:
        return None, False, None, "source_timestamp_missing"
    if isinstance(wert, datetime):
        return wert, wert.tzinfo is not None, wert.isoformat(), None
    text = str(wert).strip()
    if not text:
        return None, False, text, "source_timestamp_missing"
    try:
        geparst = datetime.fromisoformat(text.replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        return None, False, text, "source_timestamp_invalid"
    return geparst, geparst.tzinfo is not None, text, None


def _as_aware(dt, source_timezone):
    """Attach a timezone only when it is proven; otherwise (None, False)."""
    if dt.tzinfo is not None:
        return dt, True
    zone = (source_timezone or "").strip()
    if zone.lower() in _UTC_NAMES:
        return dt.replace(tzinfo=UTC), True
    return None, False


def assess_source_quality(
    *,
    source_timestamp,
    source_timezone=None,
    fetched_at=None,
    now=None,
    max_age_seconds=None,
    multiplier=None,
    multiplier_source=None,
    future_tolerance_seconds=DEFAULT_FUTURE_TOLERANCE_S,
    snapshot_id=None,
):
    """Decide whether this snapshot may be handed to automated EVA use.

    ``source_timestamp``  raw value from the source (string or datetime).
    ``source_timezone``   the PROVEN zone of a naive stamp, or None/unknown.
    ``fetched_at``        our own fetch time - recorded, never used as proof.
    ``now``              aware datetime of the check (the caller owns the clock).
    ``max_age_seconds``  limit; None means the age check is disabled.
    ``multiplier``       contract multiplier as delivered by the source.
    ``snapshot_id``      optional identity of the snapshot this refers to.

    Returns a plain dict, JSON-ready.
    """
    gruende = []

    zeit, aware, roh, fehler = _parse_timestamp(source_timestamp)
    if fehler:
        gruende.append(_reason(fehler))
        zeit_utc, zeit_ok = None, False
    else:
        zeit_utc, zeit_ok = _as_aware(zeit, source_timezone)
        if not zeit_ok:
            gruende.append(_reason("source_timezone_unproven"))

    if source_timestamp in (None, "") and fetched_at is not None:
        # Our own fetch time proves when WE asked, not when the source measured.
        gruende.append(_reason("fetched_at_is_not_a_source_time"))

    alter = None
    if zeit_ok:
        jetzt = now
        if not isinstance(jetzt, datetime):
            gruende.append(_reason("check_clock_missing"))
        else:
            if jetzt.tzinfo is None:
                # A naive clock would smuggle in exactly the assumption this
                # module exists to refuse.
                gruende.append(_reason("check_clock_missing"))
            else:
                alter = (jetzt - zeit_utc).total_seconds()
                if alter < -abs(future_tolerance_seconds):
                    gruende.append(_reason("source_timestamp_in_future"))
                elif max_age_seconds is not None and alter > max_age_seconds:
                    gruende.append(_reason("source_timestamp_stale"))

    if multiplier is None:
        gruende.append(_reason("contract_multiplier_unknown"))
    elif isinstance(multiplier, bool) or not isinstance(multiplier, (int, float)) \
            or not (multiplier > 0):
        gruende.append(_reason("contract_multiplier_invalid"))

    if max_age_seconds is None:
        gruende.append(_reason("age_check_disabled"))

    codes = [g["code"] for g in gruende]
    # Precedence, most severe first. `disabled` outranks `unknown` on purpose:
    # when the operator switched the age check off, that is the dominant fact -
    # reporting "unknown" would read like a data problem and hide the switch.
    # Broken data still outranks both. The full reason list is always attached,
    # so nothing is suppressed by this ordering.
    if any(code in _INVALID for code in codes):
        status = STATUS_INVALID
    elif "age_check_disabled" in codes:
        status = STATUS_DISABLED
    elif any(code in _UNKNOWN for code in codes):
        status = STATUS_UNKNOWN
    elif "source_timestamp_stale" in codes:
        status = STATUS_STALE
    else:
        status = STATUS_OK

    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        # The single word a caller may act on. Every other state is a "no".
        "eligible": status == STATUS_OK,
        "reasons": gruende,
        "snapshot": {
            "id": snapshot_id,
            "source_timestamp_raw": roh,
            "source_timestamp_utc": zeit_utc.isoformat() if zeit_ok else None,
            "source_timezone": _zone_name(zeit_utc, aware, source_timezone),
            "source_age_seconds": alter,
        },
        "checks": {
            "age_limit_seconds": _json_scalar(max_age_seconds),
            "age_check_disabled": max_age_seconds is None,
            "fetched_at_utc": _iso(fetched_at),
            "checked_at_utc": _iso(now),
            "future_tolerance_seconds": _json_scalar(future_tolerance_seconds),
        },
        "contract": {
            "multiplier": _json_scalar(multiplier),
            "multiplier_source": multiplier_source,
            "multiplier_proven": isinstance(multiplier, (int, float))
            and not isinstance(multiplier, bool) and multiplier > 0,
        },
    }


def _zone_name(zeit_utc, aware, source_timezone):
    """Name the zone we actually used: the value's offset, or the proven one."""
    if aware:
        versatz = zeit_utc.utcoffset()
        gesamt = int(versatz.total_seconds())
        vorzeichen = "+" if gesamt >= 0 else "-"
        gesamt = abs(gesamt)
        return f"{vorzeichen}{gesamt // 3600:02d}:{gesamt % 3600 // 60:02d}"
    zone = (source_timezone or "").strip()
    if zone.lower() in _UTC_NAMES:
        return "UTC"
    return "unknown"


def _iso(wert):
    if isinstance(wert, datetime):
        return wert.isoformat()
    return wert if isinstance(wert, str) else None


def snapshot_identity(ticker, expiration, quotes, source_timestamp=None):
    """Stable id for one fetched snapshot, so quality and maths provably agree.

    Derived from the whole ordered chain: instrument, feed stamp, and for every
    row the symbol plus its price inputs. Two fetches of the same chain therefore
    differ as soon as a quote moves, while the quality gate and the spread engine
    - both fed from the same list in the same request - carry the same id.
    """
    # The whole chain, not a boundary sample: two fetches a second apart can
    # share row count, newest stamp and first/last symbol while a quote in the
    # middle has moved. Prices are part of the identity for that reason.
    teile = [str(ticker or ""), str(expiration or ""), str(source_timestamp or "")]
    for q in quotes or []:
        asset = getattr(q, "asset", None)
        teile.append(":".join(str(_json_scalar(x)) for x in (
            getattr(asset, "symbol", ""),
            getattr(q, "bid", ""), getattr(q, "ask", ""),
            getattr(q, "iv", ""), getattr(q, "volume", ""),
            getattr(q, "open_interest", ""),
        )))
    return hashlib.sha256("|".join(teile).encode("utf-8")).hexdigest()[:16]
