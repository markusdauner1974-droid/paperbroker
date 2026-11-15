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
    now_fn: Callable[[], Any]
    trend_for_screener: Callable[[str], Any]


def context() -> FactoryContext:
    """The context of the app serving the current request."""
    return current_app.extensions[CONTEXT_KEY]
