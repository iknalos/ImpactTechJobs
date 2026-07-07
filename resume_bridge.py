"""Local Claude Code bridge for the Elder Tech Jobs resume tailor.

Runs a tiny HTTP server on 127.0.0.1:8765. The dashboard at
https://iknalos.github.io/ElderTechJobs/resume.html calls it to generate
resumes through YOUR local Claude Code CLI (subscription, no API key).

Start it with start_resume_bridge.bat and leave the window open while
you're applying to jobs. Ctrl+C to stop.
"""

import json
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# applied / not-applicable lists, mirrored from the browser so a browser
# data wipe can never lose them again
STATE_FILE = Path(__file__).parent / "tracker_state.json"

PORT = 8765
ALLOWED_ORIGINS = {"https://iknalos.github.io", "null"}  # site + local file://
# --tools "" + --disallowedTools "mcp__*" strip ALL tool access: the CLI can
# only generate text. It cannot read files, run commands, or reach the web,
# no matter what a request asks for.
CLAUDE_CMD = 'claude -p --output-format text --tools "" --disallowedTools "mcp__*"'
TIMEOUT = 420  # seconds; resume generation typically takes 30-90s


def run_claude(prompt):
    r = subprocess.run(CLAUDE_CMD, shell=True, input=prompt,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "claude CLI failed").strip()[:300])
    return r.stdout.strip()


class Handler(BaseHTTPRequestHandler):

    def _cors(self):
        origin = self.headers.get("Origin", "")
        allow = origin if (origin in ALLOWED_ORIGINS
                           or origin.startswith("http://localhost")
                           or origin.startswith("http://127.0.0.1")) else "https://iknalos.github.io"
        self.send_header("Access-Control-Allow-Origin", allow)
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-ETJ")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _guard(self):
        """Shared auth for state-changing endpoints; True if request allowed."""
        if self.headers.get("X-ETJ") != "1":
            self._json(403, {"error": "missing X-ETJ header"})
            return False
        origin = self.headers.get("Origin", "")
        if origin and origin not in ALLOWED_ORIGINS and \
                not origin.startswith(("http://localhost", "http://127.0.0.1")):
            self._json(403, {"error": "origin not allowed"})
            return False
        return True

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True, "engine": "claude-code-local",
                             "state": STATE_FILE.exists()})
        elif self.path == "/state":
            try:
                state = json.loads(STATE_FILE.read_text(encoding="utf-8")) \
                    if STATE_FILE.exists() else {}
            except Exception:
                state = {}
            self._json(200, {"applied": state.get("applied", []),
                             "dismissed": state.get("dismissed", [])})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/state":
            if not self._guard():
                return
            try:
                n = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(n).decode("utf-8"))
                state = {"applied": list(data.get("applied", [])),
                         "dismissed": list(data.get("dismissed", []))}
                tmp = STATE_FILE.with_suffix(".tmp")
                tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
                tmp.replace(STATE_FILE)
                self._json(200, {"ok": True,
                                 "applied": len(state["applied"]),
                                 "dismissed": len(state["dismissed"])})
            except Exception as e:  # noqa: BLE001
                self._json(500, {"error": str(e)[:300]})
            return
        if self.path != "/generate":
            self._json(404, {"error": "not found"})
            return
        if not self._guard():
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n).decode("utf-8"))
            prompt = (data.get("system", "") + "\n\n" + data.get("user", "")).strip()
            if not prompt:
                self._json(400, {"error": "empty prompt"})
                return
            print("  -> generating (%d chars)..." % len(prompt), flush=True)
            text = run_claude(prompt)
            print("  <- done (%d chars)" % len(text), flush=True)
            self._json(200, {"text": text})
        except subprocess.TimeoutExpired:
            self._json(504, {"error": "claude CLI timed out"})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": str(e)[:300]})

    def log_message(self, fmt, *args):  # quieter default logging
        pass


if __name__ == "__main__":
    print("Elder Tech Jobs resume bridge running at http://127.0.0.1:%d" % PORT)
    print("Keep this window open while using the Generate button on the site.")
    print("Stop with Ctrl+C.")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
