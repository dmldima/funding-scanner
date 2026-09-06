#!/usr/bin/env python3
"""Tests for which failures _request() retries.

The retry list used to be (URLError, TimeoutError, JSONDecodeError). None of
those covers http.client.IncompleteRead, which does not descend from URLError,
so a body truncated mid-transfer escaped the handler on the first attempt and
cost the whole snapshot. Observed in production, once, in venue_status.csv:

    mexc:perp   FAIL IncompleteRead(541020 bytes read)
"""

import http.client
import json
import ssl
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import venues
from venues import VenueError


class _Body:
    """Minimal stand-in for the object urlopen() yields as a context manager."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


class TestRequestRetry(unittest.TestCase):
    def setUp(self):
        # The real backoff sleeps ~7s across three attempts.
        patcher = mock.patch.object(venues.time, "sleep")
        self.addCleanup(patcher.stop)
        patcher.start()

    def _run(self, side_effect):
        with mock.patch.object(venues.urllib.request, "urlopen") as urlopen:
            urlopen.side_effect = side_effect
            try:
                return venues._request("https://example.test/x"), urlopen.call_count
            except VenueError as exc:
                return exc, urlopen.call_count

    def test_transient_failures_are_retried_then_succeed(self):
        """Each of these must not escape the first attempt."""
        ok = _Body(b'{"ok": 1}')
        transient = [
            http.client.IncompleteRead(b"partial"),   # the one that got through
            http.client.RemoteDisconnected("closed"),
            ConnectionResetError("peer reset"),
            ssl.SSLEOFError("eof"),
            urllib.error.URLError("dns"),
            TimeoutError("slow"),
            json.JSONDecodeError("bad", "", 0),
        ]
        for exc in transient:
            with self.subTest(exc=type(exc).__name__):
                result, calls = self._run([exc, ok])
                self.assertEqual(result, {"ok": 1})
                self.assertEqual(calls, 2)

    def test_transient_failure_gives_up_after_three_attempts(self):
        exc = http.client.IncompleteRead(b"partial")
        result, calls = self._run([exc, exc, exc])
        self.assertIsInstance(result, VenueError)
        self.assertIn("failed after 3 attempts", str(result))
        self.assertEqual(calls, 3)

    def test_geoblock_and_client_errors_still_fail_fast(self):
        """Retrying a 451 or a 404 only delays the same answer."""
        for code, marker in ((451, "geo-blocked"), (404, "HTTP 404")):
            with self.subTest(code=code):
                err = urllib.error.HTTPError(
                    "https://example.test/x", code, "no", {}, None)
                result, calls = self._run([err, _Body(b"{}")])
                self.assertIsInstance(result, VenueError)
                self.assertIn(marker, str(result))
                self.assertEqual(calls, 1)

    def test_server_errors_are_retried(self):
        err = urllib.error.HTTPError(
            "https://example.test/x", 503, "busy", {}, None)
        result, calls = self._run([err, _Body(b'{"ok": 1}')])
        self.assertEqual(result, {"ok": 1})
        self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
