"""The factory state that the route modules share.

``create_app`` builds the adapters, the caches and the clock once and puts them
here; a route module reads them from the app instead of closing over the
factory locals. One object, one place to look.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from flask import current_app

CONTEXT_KEY = "paperbroker"


@dataclass(frozen=True)
class FactoryContext:
    broker: Any
    screener: Any
    earnings_adapter: Any
    gap_adapter: Any
    next_earnings_adapter: Any
    trend_display: Any
    now_fn: Callable[[], Any]
    trend_for_screener: Callable[[str], Any]
    #: The hand-curated report dates (``hand_calendar.py``) and the announcement
    #: feed beside them (``earnings_feed.py``). Both default to ``None`` so a
    #: context built by an old test keeps working: no store, no signal - the
    #: funnel then behaves exactly as before this feature.
    hand_calendar: Any = None
    earnings_feed: Any = None
    #: Die Website-Auskunft zu einem Kuerzel (``company_profile.web_url``). Sie
    #: steht hier, damit ein Test sie ersetzen kann - ein Detailblock darf nicht
    #: am Netz haengen.
    company_profile: Any = None


def context() -> FactoryContext:
    """The context of the app serving the current request."""
    return current_app.extensions[CONTEXT_KEY]
