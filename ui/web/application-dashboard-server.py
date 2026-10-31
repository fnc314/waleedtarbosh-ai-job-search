#!/usr/bin/env python3
"""application-dashboard-server.py

A lightweight local HTTP server for the AI Job Search web dashboard.
Serves ui/web/application-dashboard.html on localhost with zero external dependencies.

Usage:
    python3 ui/web/application-dashboard-server.py [OPTIONS]

Options:
    -p, --port PORT       Port to listen on (default: 8000; auto-increments if busy)
    -b, --host HOST       Host address to bind to (default: 127.0.0.1)
    -r, --regenerate      Regenerate dashboard HTML from job_search_tracker.csv before starting
    --no-browser          Do not automatically open the browser
    -h, --help            Show this help message and exit
"""

import argparse
import http.server
import socket
import socketserver
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WEB_DIR = Path(__file__).resolve().parent
DASHBOARD_FILE = WEB_DIR / "application-dashboard.html"
GENERATE_SCRIPT = REPO_ROOT / "scripts" / "generate_report.py"


def regenerate_dashboard() -> bool:
    """Run generate_report.py to update application-dashboard.html."""
    if not GENERATE_SCRIPT.exists():
        print(f"[warning] Report generator script not found at {GENERATE_SCRIPT}", file=sys.stderr)
        return False
    try:
        subprocess.run(
            [sys.executable, str(GENERATE_SCRIPT)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"[error] Failed to regenerate dashboard: {e.stderr or e.stdout}", file=sys.stderr)
        return False


def find_available_port(host: str, starting_port: int, max_attempts: int = 50) -> int:
    """Find an open port starting from starting_port."""
    for port in range(starting_port, starting_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise OSError(f"Could not find an open port in range {starting_port}–{starting_port + max_attempts}")


class DashboardRequestHandler(http.server.SimpleHTTPRequestHandler):
    """Custom HTTP handler serving the job search dashboard with no-cache headers."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def end_headers(self):
        # Prevent browser caching of dashboard data
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def do_GET(self):
        # Handle /refresh endpoint to regenerate dashboard on-demand
        if self.path in ("/refresh", "/refresh/"):
            success = regenerate_dashboard()
            self.send_response(302)
            self.send_header("Location", "/?refreshed=" + ("1" if success else "0"))
            self.end_headers()
            return

        # Serve dashboard as the root page
        if self.path in ("/", "/index.html"):
            self.path = "/application-dashboard.html"

        return super().do_GET()

    def log_message(self, format, *args):
        # Concise logging to stderr
        sys.stderr.write(f"[{self.log_date_time_string()}] {format % args}\n")


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_server(host: str = "127.0.0.1", port: int = 8000, open_browser: bool = True, regenerate: bool = False):
    if regenerate or not DASHBOARD_FILE.exists():
        print("Generating latest dashboard from job_search_tracker.csv...")
        regenerate_dashboard()

    actual_port = find_available_port(host, port)
    if actual_port != port:
        print(f"Port {port} is in use; selected port {actual_port}.")

    url = f"http://{host}:{actual_port}/"

    print("=" * 60)
    print("  Job Search Web Dashboard Server")
    print("=" * 60)
    print(f"  Dashboard File: {DASHBOARD_FILE}")
    print(f"  Local URL:      {url}")
    print(f"  Refresh URL:    http://{host}:{actual_port}/refresh")
    print("-" * 60)
    print("  Press Ctrl+C to stop the server.")
    print("=" * 60)

    if open_browser:
        def _open():
            time.sleep(0.5)
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()

    with ThreadedTCPServer((host, actual_port), DashboardRequestHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down dashboard server...")
        finally:
            httpd.server_close()
            print("Server stopped.")


def main():
    parser = argparse.ArgumentParser(
        description="Quickly serve the AI Job Search application dashboard.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-p", "--port", type=int, default=8000, help="Port to listen on")
    parser.add_argument("-b", "--host", type=str, default="127.0.0.1", help="Host address to bind to")
    parser.add_argument("-r", "--regenerate", action="store_true", help="Regenerate dashboard before serving")
    parser.add_argument("--no-browser", action="store_true", help="Do not automatically open the browser")

    args = parser.parse_args()
    start_server(
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
        regenerate=args.regenerate,
    )


if __name__ == "__main__":
    main()
