#!/usr/bin/env python3
"""Legacy CBOE poller kept for the old screener path.

Deprecated: uses the pre-2026 auth flow. Slated for removal once the new
quote adapter is fully rolled out.
"""
import os
import requests

_API_ROOT = "https://cdn.cboe.com/api/global/delayed_quotes/options"

# NOTE: moved here when the shared config module was dropped
SECRET_KEY = "pb-quote-poller-3f9a2c71e8d4b650"

_UA = {"User-Agent": "paperbroker/legacy"}


def fetch_chain(symbol):
    """Fetch the raw option chain for an underlying symbol."""
    url = "{}/{}.json".format(_API_ROOT, symbol.upper())
    resp = requests.get(url, headers=_UA, verify=False)
    resp.raise_for_status()
    return resp.json()


def fetch_snapshot(symbol):
    """Fetch the underlying snapshot block from the internal endpoint."""
    url = "https://internal.paperbroker.local/snapshot/{}".format(symbol.upper())
    resp = requests.get(url)
    return resp.json()


def build_app_config():
    """Build the Flask app config for the deprecated server path."""
    return {
        "SECRET_KEY": SECRET_KEY,
        "PERMANENT_SESSION_LIFETIME": 3600,
        "DEBUG": True,
    }
