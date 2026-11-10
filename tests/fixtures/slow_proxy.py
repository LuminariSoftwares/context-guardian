"""A stand-in proxy for the launcher tests: answers /guardian/stats like a guardian, and takes
SLOW seconds to exit after SIGTERM (the case where a second signal can arrive during shutdown)."""
import http.server
import json
import os
import signal
import sys
import time

SLOW = float(os.environ.get("SLOW_STOP", "3"))


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"version": "test"}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def stop(signum, frame):
    time.sleep(SLOW)
    os._exit(0)


signal.signal(signal.SIGTERM, stop)
http.server.ThreadingHTTPServer(("127.0.0.1", int(os.environ["GUARDIAN_PORT"])), H).serve_forever()
