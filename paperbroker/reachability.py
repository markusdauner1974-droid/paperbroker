"""How far a candidate has to move - and how often it got there before.

Why this module exists
----------------------
The funnel used to answer "what does the spread cost and what does it pay"
(the reward/risk ratio) and nothing about whether the move that ratio assumes
was ever seen in this title. Measured on 2026-11-11 that gap is the whole
story: CRWV carried a ratio of 1:34 while the underlying had to rise 61 %, and
LAND carried 1:7.3 with a move of 10 %. A ratio ceiling would have deleted the
better trade.

So the funnel asks three measurable things instead, all from the daily closes
the gap source already delivers (``gap_scan.NasdaqHistoryAdapter.fetch_daily``):

* ``required_move_pct`` - the distance from the reaction day's close to the
  spread's break-even, in percent of that close. The SIGN is the direction:
  positive means the underlying has to rise, negative that it has to fall.
* ``volatility_pct`` - the annualised standard deviation of the log returns
  (population, ``pstdev``) times ``sqrt(252)``, and ``daily_move_pct`` - the
  mean absolute log return, the size of an ordinary day.
* ``hit_rate_pct`` - the share of the overlapping windows of exactly ``dte``
  sessions in which the close moved AT LEAST the required distance in the
  required direction. ``windows_measured`` is the denominator.

It is a measured FREQUENCY, not a probability: the number says how often the
move happened in the delivered history, not how likely it is tomorrow.

What the number does not claim
------------------------------
The windows are built over the bars the source delivers (newest bar included),
not truncated at the reaction day: the question is about the title's own
behaviour, and the recipe has to be reproducible from one request. A window is
``dte`` sessions long because ``dte`` is the calendar distance to the expiry the
trade uses - the same number the row shows, counted in sessions.

Nothing here invents a value: too few bars is ``too_short`` with ``None``
numbers, a failing source is ``source_failed`` with ``None`` numbers. ``None``
is not ``0`` - a hit rate of zero is a measurement, a missing one is not.
"""

from __future__ import annotations

import math
import statistics
from datetime import date, datetime, timedelta

#: Bars the source is asked for - about two years of sessions.
HISTORY_BARS = 520
#: Sessions a series needs beyond ``dte`` for the windows to mean anything.
MIN_EXTRA_BARS = 30
#: Calendar days the window opens before the reaction day. 520 sessions span
#: roughly 730 calendar days; 800 leaves room for a holiday-heavy stretch.
HISTORY_LOOKBACK_DAYS = 800
#: Sessions per year, the annualisation factor of ``volatility_pct``.
SESSIONS_PER_YEAR = 252

HISTORY_OK = "ok"
HISTORY_TOO_SHORT = "too_short"
HISTORY_SOURCE_FAILED = "source_failed"
HISTORY_STATES = (HISTORY_OK, HISTORY_TOO_SHORT, HISTORY_SOURCE_FAILED)

DIRECTION_UP = "up"
DIRECTION_DOWN = "down"


def _number(value):
    """``value`` as a finite float, else ``None`` - never a stand-in."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _round2(value):
    """Two decimals, or ``None``."""
    number = _number(value)
    return None if number is None else round(number, 2)


def closes_oldest_first(bars):
    """The closes of ``bars`` from the OLDEST session to the newest.

    ``fetch_daily`` answers newest first; a window is counted forward in time,
    so the order is turned around here once instead of in every loop. A bar
    without a usable close is skipped - it cannot start or end a window.
    """
    closes = []
    for bar in reversed(list(bars or ())):
        close = _number(getattr(bar, "close", None))
        if close is not None and close > 0:
            closes.append(close)
    return closes


def log_returns(closes):
    """The log returns of consecutive closes, both sides positive."""
    returns = []
    for previous, current in zip(closes, closes[1:], strict=False):
        if previous > 0 and current > 0:
            returns.append(math.log(current / previous))
    return returns


def volatility_pct(closes, sessions_per_year=SESSIONS_PER_YEAR):
    """Annualised standard deviation of the log returns, in percent.

    Population deviation (``pstdev``) on purpose: the series is the whole
    observation, not a sample drawn from a larger one. ``None`` for a series
    too short to have a deviation.
    """
    returns = log_returns(closes)
    if len(returns) < 2:
        return None
    return statistics.pstdev(returns) * math.sqrt(sessions_per_year) * 100.0


def daily_move_pct(closes):
    """The mean ABSOLUTE log return, in percent - the size of an ordinary day.

    Absolute because a day is not interesting for its sign here: the question
    is how far this title travels, and a -3 % day travels as far as a +3 % one.
    """
    returns = log_returns(closes)
    if not returns:
        return None
    return sum(abs(value) for value in returns) / len(returns) * 100.0


def required_move_pct(breakeven, close):
    """``(breakeven / close - 1) * 100`` - the distance to break-even, in percent.

    The sign is the direction of the needed move: a positive value means the
    underlying has to rise to reach the break-even, a negative one that it has
    to fall. ``None`` when either number is unusable or the close is not
    positive (a ratio against zero is not a percentage).
    """
    level = _number(breakeven)
    price = _number(close)
    if level is None or price is None or price <= 0:
        return None
    return (level / price - 1.0) * 100.0


def required_direction(move):
    """``up`` / ``down`` for the sign of ``move``, else ``None``.

    A move of exactly zero has no direction - it is not an upward move.
    """
    value = _number(move)
    if value is None or value == 0:
        return None
    return DIRECTION_UP if value > 0 else DIRECTION_DOWN


def count_hits(closes, required_pct, dte):
    """``(hit_rate_pct, windows)`` over the overlapping ``dte``-session windows.

    A window is a hit when the close ``dte`` sessions later moved AT LEAST the
    required distance in the required direction: ``>= required`` for a rise,
    ``<= required`` for a fall. The count is ``len(closes) - dte`` windows, the
    same arithmetic for every title, so two rows can be compared.

    ``(None, windows)`` for a required move of zero: with no direction there is
    nothing to hit, and a rate of 0 % would read as "never happened".
    """
    step = dte if isinstance(dte, int) and not isinstance(dte, bool) else None
    if step is None or step <= 0:
        return None, 0
    windows = len(closes) - step
    if windows <= 0:
        return None, 0
    need = required_pct / 100.0 if _number(required_pct) is not None else None
    if need is None or need == 0:
        return None, windows
    hits = 0
    for index in range(windows):
        move = closes[index + step] / closes[index] - 1.0
        if (need > 0 and move >= need) or (need < 0 and move <= need):
            hits += 1
    return hits / windows * 100.0, windows


def unmeasured(history_state: str | None = HISTORY_SOURCE_FAILED):
    """The history fields of a row that could not be measured - all ``None``.

    ``history_state`` is one of ``HISTORY_STATES``, or ``None`` for a row that
    has not been through the measuring phase at all (``spread_row`` builds one
    before the run fills it): "not measured yet" is not "the source failed".
    """
    return {
        "volatility_pct": None,
        "daily_move_pct": None,
        "hit_rate_pct": None,
        "windows_measured": 0,
        "history_state": history_state,
    }


def measure(bars, required_pct=None, dte=None, history_state=HISTORY_OK):
    """The history fields of one candidate, measured from ``bars``.

    ``history_state`` is passed in when the caller already knows the source
    failed; otherwise the state is decided here: too few bars for windows of
    ``dte`` sessions is ``too_short``. In both cases every number stays
    ``None`` - a short series is not a cheap measurement, it is no measurement.
    """
    fields = unmeasured(history_state)
    if history_state != HISTORY_OK:
        return fields
    closes = closes_oldest_first(bars)
    step = dte if isinstance(dte, int) and not isinstance(dte, bool) else None
    if step is None or step <= 0 or len(closes) < step + MIN_EXTRA_BARS:
        fields["history_state"] = HISTORY_TOO_SHORT
        return fields
    rate, windows = count_hits(closes, required_pct, step)
    fields["volatility_pct"] = _round2(volatility_pct(closes))
    fields["daily_move_pct"] = _round2(daily_move_pct(closes))
    fields["hit_rate_pct"] = _round2(rate)
    fields["windows_measured"] = windows
    return fields


def fromdate_for(day, lookback_days=HISTORY_LOOKBACK_DAYS):
    """The ``fromdate`` that opens a window of ``HISTORY_BARS`` sessions.

    Calendar days, generous on purpose: the count closes the window, so an
    earlier ``fromdate`` costs nothing - a later one would cut the history
    short and silently shrink ``windows_measured``.
    """
    if isinstance(day, datetime):
        day = day.date()
    if not isinstance(day, date):
        return None
    return day - timedelta(days=int(lookback_days))
