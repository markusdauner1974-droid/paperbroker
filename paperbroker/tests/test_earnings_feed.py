"""The announcement feed: read the headline, show it, stop there.

The tests run against STATIC RSS fixtures - no socket, no article, no consent
wall. What is pinned:

* the filter takes a scheduling announcement and leaves an analysis piece,
* a feed without such an item is ``None`` (not the first item, not a guess),
* broken XML is ``None``, never an exception,
* the adapter asks ONCE per company name and remembers the answer for 12 h,
* a failing transport or a non-200 status is "no signal" - a scan never dies of
  a news site.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from paperbroker import earnings_feed as feed

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>"Acme Corp" schedules earnings - Google News</title>
  <item>
    <title>Acme Corp stock: why analysts stay cautious into the quarter - Motley Fool</title>
    <link>https://news.example.com/analyse</link>
  </item>
  <item>
    <title>Acme Corp (ACME) to Report Fourth Quarter 2026 Earnings on December 10 - Reuters</title>
    <link>https://news.example.com/ankuendigung</link>
  </item>
</channel></rss>
"""

OHNE_TERMIN = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item><title>Acme Corp announces a new board member - PR Newswire</title>
    <link>https://news.example.com/personalie</link></item>
</channel></rss>
"""

JETZT = datetime(2026, 11, 17, 15, 0)


class ParserTest(unittest.TestCase):
    def test_a_scheduling_announcement_is_the_signal(self):
        signal = feed.parse_feed(RSS)
        self.assertIsNotNone(signal)
        self.assertEqual("https://news.example.com/ankuendigung", signal["link"])
        self.assertIn("to Report Fourth Quarter", signal["title"])

    def test_an_analysis_piece_is_not_a_signal(self):
        self.assertIsNone(feed.parse_feed(OHNE_TERMIN))

    def test_broken_or_empty_xml_is_no_signal(self):
        for text in ("", "   ", "<rss><channel>", "kein xml", None, b"\xff\xfe\x00"):
            self.assertIsNone(feed.parse_feed(text), repr(text)[:40])

    def test_bytes_are_decoded(self):
        self.assertIsNotNone(feed.parse_feed(RSS.encode("utf-8")))

    def test_the_filter_needs_a_verb_and_a_noun(self):
        for titel in ("Acme schedules Q3 earnings call",
                      "Acme to report fiscal 2027 results",
                      "Acme sets date for fourth quarter webcast",
                      "Acme will announce Q1 financial results"):
            self.assertTrue(feed.looks_like_schedule(titel), titel)
        for titel in ("Acme quarterly results fall short of estimates",
                      "Acme earnings: three things to watch",
                      "Acme announces new board member",
                      ""):
            self.assertFalse(feed.looks_like_schedule(titel), titel)

    def test_the_query_carries_the_company_name_not_only_the_ticker(self):
        url = feed.query_url("Acme Corp")
        self.assertIn("news.google.com/rss/search", url)
        self.assertIn("Acme", url)
        self.assertIn("schedules", url)
        self.assertNotIn(" ", url)


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = os.path.join(self._tmp.name, "feed.json")
        self.calls = []
        self.clock = JETZT

    def tearDown(self):
        self._tmp.cleanup()

    def _adapter(self, status=200, body=RSS, raise_error=None):
        def transport(url, headers, timeout):
            self.calls.append({"url": url, "headers": headers, "timeout": timeout})
            if raise_error is not None:
                raise raise_error
            return status, body
        return feed.EarningsFeedAdapter(transport=transport, cache_path=self.cache,
                                        now_fn=lambda: self.clock)

    def test_a_signal_carries_title_and_link(self):
        signal = self._adapter().signal("Acme Corp")
        self.assertEqual("https://news.example.com/ankuendigung", signal["link"])
        self.assertEqual(1, len(self.calls))
        self.assertLessEqual(self.calls[0]["timeout"], 4.0)
        self.assertIn("rss+xml", self.calls[0]["headers"]["Accept"])

    def test_the_answer_is_remembered_for_twelve_hours(self):
        adapater = self._adapter()
        self.assertIsNotNone(adapater.signal("Acme Corp"))
        self.clock = JETZT + timedelta(hours=11)
        self.assertIsNotNone(adapater.signal("Acme Corp"))
        self.assertEqual(1, len(self.calls), "innerhalb der TTL kein zweiter Abruf")
        self.clock = JETZT + timedelta(hours=13)
        adapater.signal("Acme Corp")
        self.assertEqual(2, len(self.calls), "nach der TTL wird erneut gefragt")

    def test_no_signal_is_remembered_too(self):
        adapter = self._adapter(body=OHNE_TERMIN)
        self.assertIsNone(adapter.signal("Acme Corp"))
        self.assertIsNone(adapter.signal("Acme Corp"))
        self.assertEqual(1, len(self.calls))

    def test_a_failing_source_is_no_signal(self):
        self.assertIsNone(self._adapter(status=503).signal("Acme Corp"))
        self.assertIsNone(self._adapter(raise_error=TimeoutError("zu langsam")).signal("Acme Corp"))
        self.assertIsNone(self._adapter(body="kein xml").signal("Acme Corp"))

    def test_an_empty_name_costs_no_request(self):
        adapter = self._adapter()
        self.assertIsNone(adapter.signal(""))
        self.assertIsNone(adapter.signal("  "))
        self.assertEqual([], self.calls)

    def test_the_cache_file_keeps_its_shape(self):
        self._adapter().signal("Acme Corp")
        inhalt = json.loads(Path(self.cache).read_text(encoding="utf-8"))
        self.assertEqual(1, inhalt["version"])
        self.assertEqual("google_news_rss", inhalt["source"])
        self.assertEqual({"title", "link", "fetched_at"},
                         set(inhalt["signals"]["acme corp"]))

    def test_a_broken_cache_file_is_ignored(self):
        Path(self.cache).write_text("{kaputt", encoding="utf-8")
        self.assertIsNotNone(self._adapter().signal("Acme Corp"))
        self.assertEqual(1, len(self.calls))
