#!/usr/bin/env python3
"""PSX X-Req-Id token handling, POST support and the published-history parser."""

import io
import unittest
import urllib.error
from unittest import mock

import psx_http
import psx_market_data as md

PAGE = '<html><script>window.__ps = {"_k":"AbCdEf0123456789xyz","v":1};</script></html>'


class Resp:
    def __init__(self, body):
        self.body = body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body

    def info(self):
        return {}


class TestToken(unittest.TestCase):

    def setUp(self):
        psx_http._token.update(value=None, at=0.0, failed_at=0.0)
        self.requests = []

    def test_extract_token(self):
        self.assertEqual(psx_http.extract_token(PAGE), "AbCdEf0123456789xyz")
        self.assertIsNone(psx_http.extract_token("<html>no token</html>"))
        self.assertIsNone(psx_http.extract_token('<script>window.__ps = {"_k":"short"};</script>'))

    def fake(self, rejects_first=False):
        tokens = iter(["AbCdEf0123456789xyz", "ZzZzZz9876543210new"])

        def urlopen(req, timeout, context):
            url = req.full_url
            hdr = dict(req.header_items())
            self.requests.append((req.get_method(), url, hdr.get("X-req-id"), req.data))
            if url == psx_http.TOKEN_PAGE:
                return Resp('<script>window.__ps = {"_k":"%s"};</script>' % next(tokens))
            if rejects_first and hdr.get("X-req-id") == "AbCdEf0123456789xyz":
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b""))
            return Resp("DATA")
        return urlopen

    def test_token_sent_on_dps_requests_and_cached(self):
        with mock.patch("urllib.request.urlopen", self.fake()):
            self.assertEqual(psx_http.fetch("https://dps.psx.com.pk/timeseries/eod/OGDC"), "DATA")
            psx_http.fetch("https://dps.psx.com.pk/screener")
        data_reqs = [r for r in self.requests if r[1] != psx_http.TOKEN_PAGE]
        self.assertEqual([r[2] for r in data_reqs], ["AbCdEf0123456789xyz"] * 2)
        self.assertEqual(sum(1 for r in self.requests if r[1] == psx_http.TOKEN_PAGE), 1)

    def test_403_refreshes_token_once(self):
        with mock.patch("urllib.request.urlopen", self.fake(rejects_first=True)):
            self.assertEqual(psx_http.fetch("https://dps.psx.com.pk/historical", retries=1,
                                            data={"symbol": "OGDC"}), "DATA")
        last = self.requests[-1]
        self.assertEqual((last[0], last[2], last[3]), ("POST", "ZzZzZz9876543210new", b"symbol=OGDC"))

    def test_non_psx_hosts_get_no_token(self):
        with mock.patch("urllib.request.urlopen", self.fake()):
            psx_http.fetch("https://api.telegram.org/x")
        self.assertEqual(self.requests, [("GET", "https://api.telegram.org/x", None, None)])


class TestHistoricalParser(unittest.TestCase):

    def test_parse(self):
        html = """<table><thead><tr><th>DATE</th><th>OPEN</th><th>HIGH</th><th>LOW</th><th>CLOSE</th><th>VOLUME</th></tr></thead>
        <tbody><tr><td>Oct 03, 2026</td><td>318.90</td><td>321.00</td><td>317.50</td><td>319.52</td><td>1,712,061</td></tr>
        <tr><td>Oct 02, 2026</td><td>317.80</td><td>319.00</td><td>316.10</td><td>318.41</td><td>1,143,864</td></tr>
        <tr><td>Oct 01, 2026</td><td>10</td><td>9</td><td>11</td><td>10</td><td>5</td></tr></tbody></table>"""
        bars = md.parse_historical_html(html)
        self.assertEqual([b["date"] for b in bars], ["2026-10-02", "2026-10-03"])  # oldest first, bad row dropped
        self.assertEqual((bars[1]["high"], bars[1]["low"], bars[1]["volume"], bars[1]["hlEstimated"]),
                         (321.0, 317.5, 1712061.0, False))

    def test_live_board_without_ohl(self):
        html = """<table><tr><th>SYMBOL</th><th>LDCP</th><th>CURRENT</th><th>CHANGE</th><th>VOLUME</th></tr>
        <tr><td>OGDC</td><td>317.49</td><td>319.00</td><td>1.51</td><td>2,000,000</td></tr></table>"""
        rows = md.parse_market_watch_html(html)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["price"], rows[0]["volume"], rows[0]["high"]), (319.0, 2e6, None))


if __name__ == "__main__":
    unittest.main()
