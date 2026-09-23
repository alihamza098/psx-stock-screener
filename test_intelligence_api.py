import unittest
import server
from urllib.parse import urlparse

class TestIntelligenceApiRoutes(unittest.TestCase):
    def test_all_intelligence_endpoints_status_200(self):
        endpoints = [
            '/api/intelligence/summary',
            '/api/intelligence/live-events?limit=50',
            '/api/intelligence/patterns',
            '/api/intelligence/predictions?limit=20',
            '/api/intelligence/suggested-shares',
            '/api/intelligence/calibration-runs?limit=10',
            '/api/intelligence/calibration-curve',
            '/api/intelligence/circuit-runners',
            '/api/intelligence/learning-stats',
            '/api/intelligence/calibration-brier',
            '/api/intelligence/evaluation-audit',
            '/api/intelligence/data-quality'
        ]

        class MockHandler:
            def __init__(self, path):
                self.path = path
                self.status = 200
                self.data = None
                self.headers = {}
            def _send_json(self, data, status=200):
                self.data = data
                self.status = status
            def send_error(self, code, msg=''):
                self.status = code
                self.data = msg

        for ep in endpoints:
            h = MockHandler(ep)
            server.PSXHandler.do_GET(h)
            self.assertEqual(h.status, 200, f"Endpoint {ep} failed with status {h.status}: {h.data}")
            self.assertTrue(isinstance(h.data, dict), f"Endpoint {ep} did not return dict")
            self.assertTrue(h.data.get('success'), f"Endpoint {ep} success != True: {h.data}")

if __name__ == '__main__':
    unittest.main()
