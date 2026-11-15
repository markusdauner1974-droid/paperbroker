"""Liveness route, moved 1:1 out of ``server.create_app``."""

from flask import jsonify


def healthz():
    # liveness only: no CBOE, no xang - must stay green in LAN tests
    # even when the internet is down
    return jsonify(status="ok")


def register(app) -> None:
    app.add_url_rule("/healthz", endpoint="healthz", view_func=healthz,
                     methods=["GET"])
