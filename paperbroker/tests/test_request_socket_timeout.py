"""The analytics module sets a process-wide socket default - the request socket must not keep it."""

import unittest

from paperbroker.server import create_app


class FakeConnection:
    def __init__(self):
        self.timeout = 8.0
        self.calls = []

    def settimeout(self, value):
        self.calls.append(value)
        self.timeout = value


class RequestSocketTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()

    def test_the_request_socket_does_not_keep_the_outbound_limit(self):
        """Kody: an accepted socket inherits setdefaulttimeout and would cut a response mid-send."""
        for key in ("gunicorn.socket", "werkzeug.socket"):
            with self.subTest(env=key):
                connection = FakeConnection()
                self.client.get("/healthz", environ_overrides={key: connection})
                self.assertEqual([None], connection.calls, f"{key} was not freed")
                self.assertIsNone(connection.timeout)

    def test_a_request_without_a_server_socket_is_harmless(self):
        response = self.client.get("/healthz")
        self.assertEqual(200, response.status_code)
