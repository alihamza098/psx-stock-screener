#!/usr/bin/env python3
"""fetch_url verifies certificates; only PSX hosts may fall back to an unverified connection."""

import os
import ssl
import unittest
import urllib.error
from unittest import mock

import server


class Resp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"ok"

    def info(self):
        return {}


class TestTlsFallback(unittest.TestCase):

    def setUp(self):
        self.calls = []
        import psx_http
        patcher = mock.patch.object(psx_http, "get_token", return_value=None)  # TLS only; token tested elsewhere
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake(self, req, timeout, context):
        self.calls.append("insecure" if context is server._INSECURE_SSL_CONTEXT else "verified")
        if context is server.SSL_CONTEXT:
            raise urllib.error.URLError(ssl.SSLCertVerificationError("incomplete chain"))
        return Resp()

    def test_psx_host_falls_back_even_with_one_retry(self):
        with mock.patch("urllib.request.urlopen", self.fake):
            self.assertEqual(server.fetch_url("https://dps.psx.com.pk/x", retries=1), "ok")
        self.assertEqual(self.calls, ["verified", "insecure"])

    def test_other_hosts_and_strict_mode_refuse(self):
        with mock.patch("urllib.request.urlopen", self.fake):
            with self.assertRaises(urllib.error.URLError):
                server.fetch_url("https://api.example.com/x", retries=1)
            with mock.patch.dict(os.environ, {"PSX_STRICT_TLS": "1"}), self.assertRaises(urllib.error.URLError):
                server.fetch_url("https://dps.psx.com.pk/x", retries=1)
        self.assertNotIn("insecure", self.calls)


if __name__ == "__main__":
    unittest.main()
