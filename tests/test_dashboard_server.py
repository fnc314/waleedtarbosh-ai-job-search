"""Unit tests for ui/web/application-dashboard-server.py."""

import socket
import sys
import threading
import time
import unittest
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "ui" / "web"))

import importlib.util
server_spec = importlib.util.spec_from_file_location(
    "application_dashboard_server",
    REPO_ROOT / "ui" / "web" / "application-dashboard-server.py",
)
server_mod = importlib.util.module_from_spec(server_spec)
server_spec.loader.exec_module(server_mod)


class DashboardServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Pick an open port
        cls.port = server_mod.find_available_port("127.0.0.1", 9500)
        cls.host = "127.0.0.1"
        cls.server = server_mod.ThreadedTCPServer(
            (cls.host, cls.port), server_mod.DashboardRequestHandler
        )
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_find_available_port(self):
        port = server_mod.find_available_port("127.0.0.1", 9600)
        self.assertIsInstance(port, int)
        self.assertGreaterEqual(port, 9600)

    def test_get_root_serves_dashboard(self):
        url = f"http://{self.host}:{self.port}/"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            content = resp.read().decode("utf-8")
            self.assertIn("Job Search Dashboard", content)
            self.assertIn("Cache-Control", resp.headers)
            self.assertIn("no-cache", resp.headers["Cache-Control"])

    def test_get_html_file_directly(self):
        url = f"http://{self.host}:{self.port}/application-dashboard.html"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            content = resp.read().decode("utf-8")
            self.assertIn("Job Search Dashboard", content)

    def test_refresh_endpoint_redirects(self):
        # Custom opener to prevent auto-following redirects so we can assert on 302
        class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
            def http_error_302(self, req, fp, code, msg, headers):
                return fp

        opener = urllib.request.build_opener(NoRedirectHandler)
        url = f"http://{self.host}:{self.port}/refresh"
        req = urllib.request.Request(url)
        with opener.open(req, timeout=5) as resp:
            self.assertEqual(resp.status, 302)
            self.assertTrue(resp.headers.get("Location", "").startswith("/?refreshed="))


if __name__ == "__main__":
    unittest.main()
