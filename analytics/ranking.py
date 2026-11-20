"""Weighted ranking list over a universe of shares.

TraderFox ranks its lists with two factors: the robustness check at ninety percent and the
distance from the high at ten percent, over 250 periods. This module applies that rule to a
universe of tickers. It needs prices only - no statements, no paid reports - so a list of two
hundred shares stays affordable.

The robustness points follow the fifteen criteria of the robustness check on the page. Where the
page compares against a reference group, this list cannot: it only sees the universe the caller
handed over, so the criteria are judged against their published pass values and the list says
how many of them were comparable.

Periods are trading days. The page's "distance from the high" wording leaves open whether the
high is an intraday or a closing high; here it is the highest close of the last 250 sessions,
and the column is named after that.

Identifiers are English; the strings a user reads stay German, like the rest of the page.
"""

from __future__ import annotations

import math
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

WEIGHT_ROBUSTNESS = 0.9
WEIGHT_DISTANCE = 0.1
PERIODS = 250
MAX_SYMBOLS = 200

UNIVERSE_DIR = Path(os.environ.get("AKTIENANALYSE_UNIVERSE", Path(__file__).parent / "universes"))


def load_universe(name: str) -> list[str]:
    """Read one ticker per line; everything after a hash is a comment."""
    if not name:
        return []
    path = UNIVERSE_DIR / f"{name.lower().replace('.', '')}.txt"
    if not path.is_file():
        raise ValueError(f"Unbekanntes Universum: {name}. Erwartet wird die Datei {path}.")
    symbols = []
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip().upper()
        if entry:
            symbols.append(entry)
    return symbols


def _closes(rows):
    """Closing prices, adjusted when every session carries an adjusted close."""
    column = 2 if rows and all(isinstance(row[2], (int, float)) and row[2] > 0 for row in rows) else 1
    return [float(row[column]) for row in rows
            if isinstance(row[column], (int, float)) and row[column] > 0]


def _daily_returns(prices):
    return [later / earlier - 1 for earlier, later in zip(prices, prices[1:], strict=False)
            if earlier > 0]


def _stdev(values):
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)


def _return(prices, months):
    """Total return over the given number of months, twenty-one sessions each."""
    needed = months * 21
    if len(prices) <= needed:
        return None
    return prices[-1] / prices[-1 - needed] - 1


def _weekly_returns(prices):
    return [prices[i] / prices[i - 5] - 1 for i in range(5, len(prices)) if prices[i - 5] > 0]


def _calendar_drawdowns(rows, count, key_length):
    """Worst fall within each of the last calendar groups, averaged."""
    groups = {}
    for row in rows:
        tag = str(row[0])[:key_length]
        value = row[2] if isinstance(row[2], (int, float)) and row[2] > 0 else row[1]
        if isinstance(value, (int, float)) and value > 0:
            groups.setdefault(tag, []).append(float(value))
    keys = sorted(groups)[-count:]
    if len(keys) < count:
        return None
    drawdowns = []
    for key in keys:
        peak, deepest = 0.0, 0.0
        for value in groups[key]:
            peak = max(peak, value)
            deepest = min(deepest, value / peak - 1)
        drawdowns.append(deepest)
    return sum(drawdowns) / len(drawdowns)


def _year_drawdowns(rows, count):
    return _calendar_drawdowns(rows, count, 4)


def _month_drawdowns(rows, count):
    return _calendar_drawdowns(rows, count, 7)


def _correlation(a, b):
    if len(a) < 20 or len(a) != len(b):
        return None
    mean_a, mean_b = sum(a) / len(a), sum(b) / len(b)
    cross = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b, strict=True))
    spread_a = math.sqrt(sum((x - mean_a) ** 2 for x in a))
    spread_b = math.sqrt(sum((y - mean_b) ** 2 for y in b))
    if spread_a == 0 or spread_b == 0:
        return None
    return cross / (spread_a * spread_b)


def _excess_return(prices, index, window):
    """Mean excess return on the days the index fell."""
    own = _daily_returns(prices)[-window:]
    other = _daily_returns(index)[-window:]
    length = min(len(own), len(other))
    if length < 10:
        return None
    pairs = list(zip(own[-length:], other[-length:], strict=True))
    weak = [mine - theirs for mine, theirs in pairs if theirs < 0]
    return sum(weak) / len(weak) if weak else None


def robustness(rows, index_rows) -> dict:
    """The fifteen robustness criteria, judged against their pass values."""
    prices = _closes(rows)
    index = _closes(index_rows)
    if len(prices) < 300:
        return {"points": None, "comparable": 0, "reason": "zu kurze Kursreihe"}
    returns = _daily_returns(prices)
    weekly = _weekly_returns(prices)
    criteria = [
        ("Performance 3 Jahre", _return(prices, 36), lambda value: value >= 0.20),
        ("Performance 5 Jahre", _return(prices, 60), lambda value: value >= 0.40),
        ("Performance 10 Jahre", _return(prices, 120), lambda value: value >= 0.90),
        ("Tagesvolatilität 200 Tage", _stdev(returns[-200:]), lambda value: value <= 0.02),
        ("Tagesvolatilität 500 Tage", _stdev(returns[-500:]), lambda value: value <= 0.02),
        ("Wochenvolatilität 50 Wochen", _stdev(weekly[-50:]), lambda value: value <= 0.045),
        ("Wochenvolatilität 200 Wochen", _stdev(weekly[-200:]), lambda value: value <= 0.045),
        ("Drawdown Kalenderjahre 10", _year_drawdowns(rows, 10), lambda value: value >= -0.30),
        ("Drawdown Kalenderjahre 20", _year_drawdowns(rows, 20), lambda value: value >= -0.30),
        ("Drawdown Kalendermonate 36", _month_drawdowns(rows, 36), lambda value: value >= -0.10),
        ("Drawdown Kalendermonate 100", _month_drawdowns(rows, 100), lambda value: value >= -0.10),
        ("Korrelation 20 Handelstage", _correlation(returns[-20:], _daily_returns(index)[-20:]),
         lambda value: value <= 0.35),
        ("Korrelation 65 Handelstage", _correlation(returns[-65:], _daily_returns(index)[-65:]),
         lambda value: value <= 0.35),
        ("Mehrrendite 20 Handelstage", _excess_return(prices, index, 20), lambda value: value >= 0),
        ("Mehrrendite 65 Handelstage", _excess_return(prices, index, 65), lambda value: value >= 0),
    ]
    checked = [(rule(value) if value is not None else None) for _, value, rule in criteria]
    known = [outcome for outcome in checked if outcome is not None]
    return {"points": sum(1 for outcome in known if outcome), "comparable": len(known),
            "open": [criteria[i][0] for i, outcome in enumerate(checked) if outcome is None]}


def distance_to_high(rows, periods: int = PERIODS) -> float | None:
    """Closing price relative to the highest close of the last periods; 1.0 means at the high."""
    prices = _closes(rows)
    if len(prices) < min(periods, 60):
        return None
    window = prices[-periods:]
    high = max(window)
    return prices[-1] / high if high > 0 else None


def rank_list(symbols, fetch_prices, index_rows, periods: int = PERIODS) -> dict:
    """Score every symbol and sort the list by the weighted score."""
    unique = []
    for symbol in symbols:
        clean = str(symbol).strip().upper()
        if clean and clean not in unique:
            unique.append(clean)
    if len(unique) > MAX_SYMBOLS:
        raise ValueError(f"Höchstens {MAX_SYMBOLS} Titel je Rangliste, angefordert waren "
                         f"{len(unique)}.")
    if not unique:
        raise ValueError("Das Universum ist leer.")

    def score_one(symbol):
        try:
            rows = fetch_prices(symbol)
        except Exception as error:  # a missing ticker must not stop the list
            return {"symbol": symbol, "error": str(error)[:120]}
        scored = robustness(rows, index_rows)
        distance = distance_to_high(rows, periods)
        if scored["points"] is None or distance is None:
            return {"symbol": symbol, "error": scored.get("reason") or "kein Abstand vom Hoch",
                    "comparable": scored["comparable"]}
        return {"symbol": symbol, "points": scored["points"],
                "comparable": scored["comparable"], "open": scored["open"],
                "robustness": 100 * scored["points"] / scored["comparable"],
                "distance": distance}

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(score_one, unique))
    scored_rows = [entry for entry in results if "points" in entry]
    # The distance becomes a rank within the universe: the closer to its high, the higher.
    ordered = sorted(scored_rows, key=lambda entry: entry["distance"])
    for place, entry in enumerate(ordered):
        entry["distance_rank"] = 100 * place / max(1, len(ordered) - 1) if len(ordered) > 1 else 100
        entry["score"] = (WEIGHT_ROBUSTNESS * entry["robustness"]
                          + WEIGHT_DISTANCE * entry["distance_rank"])
    ranked = sorted(scored_rows, key=lambda entry: (-entry["score"], entry["symbol"]))
    for place, entry in enumerate(ranked, start=1):
        entry["rank"] = place
    skipped = [entry for entry in results if "points" not in entry]
    return {"rows": ranked, "skipped": skipped, "periods": periods,
            "weights": {"robustness": WEIGHT_ROBUSTNESS, "distance": WEIGHT_DISTANCE},
            "universe": len(unique), "scored": len(ranked)}
