#!/usr/bin/env python3
"""Accept one simulated metric-alert POST and start the same run as run.py.

Nothing here is registered with Sentry. A real alert does not arrive on its own.
POST a metric-alert-shaped JSON body to /metric-alert. The response is 202
with a run id; the Claude session runs on a background thread.

`python run.py` stays the one-shot path and does not need this process.

If SENTRY_WEBHOOK_SECRET is set, the raw body must match Sentry-Hook-Signature
(HMAC-SHA256, hex). If it is unset, signatures are not checked.
"""

import hashlib
import hmac
import json
import os
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from run import ROOT, bounded, load_env_file, run_once

MAX_BODY = 65536
SECRET_ENV = "SENTRY_WEBHOOK_SECRET"


def signature_ok(raw, header):
    secret = os.environ.get(SECRET_ENV, "").strip()
    if not secret:
        return True
    if not header:
        return False
    presented = header.strip()
    if presented.startswith("sha256="):
        presented = presented[len("sha256=") :]
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, presented)


def metric_alert(body):
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    if not isinstance(data, dict):
        return None
    alert = data.get("metric_alert")
    if not isinstance(alert, dict):
        return None
    return alert


def alert_fields(alert):
    """Project slug and rule name only, each length-limited. Not the raw body."""
    projects = alert.get("projects") if isinstance(alert.get("projects"), list) else []
    project = projects[0] if projects and isinstance(projects[0], str) else None
    rule = alert.get("alert_rule") if isinstance(alert.get("alert_rule"), dict) else {}
    name = rule.get("name") if isinstance(rule.get("name"), str) else None
    project = bounded(project, 100) or None
    name = bounded(name, 200) or None
    return project, name


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path.split("?", 1)[0] != "/metric-alert":
            self._send(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._send(400, {"ok": False, "error": "invalid content length"})
            return
        if length > MAX_BODY:
            self._send(413, {"ok": False, "error": "body too large"})
            return
        raw = self.rfile.read(length) if length else b""
        if not signature_ok(raw, self.headers.get("Sentry-Hook-Signature")):
            self._send(401, {"ok": False, "error": "bad signature"})
            return
        try:
            body = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._send(400, {"ok": False, "error": "invalid json"})
            return
        alert = metric_alert(body)
        if alert is None:
            self._send(400, {"ok": False, "error": "expected data.metric_alert"})
            return
        project, rule_name = alert_fields(alert)
        run_id = uuid.uuid4().hex
        print("accepted", run_id, "action", body.get("action"), flush=True)

        def work():
            try:
                summary = run_once(
                    max_turns=int(os.environ.get("DICTATION_FIXER_MAX_TURNS", "20")),
                    project=project,
                    alert_name=rule_name,
                )
                print(
                    "run finished",
                    run_id,
                    "trace",
                    summary.get("trace_id"),
                    "exit",
                    summary.get("exit_code"),
                    "turns",
                    summary.get("num_turns"),
                    flush=True,
                )
            except Exception as exc:
                print("run failed", run_id, type(exc).__name__, flush=True)

        threading.Thread(target=work, name=f"run-{run_id[:8]}", daemon=False).start()
        self._send(202, {"run_id": run_id})

    def _send(self, code, payload):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        print("http", fmt % args, flush=True)


def main():
    load_env_file()
    venv_python = ROOT / ".venv" / "bin" / "python"
    if venv_python.exists() and Path(sys.prefix) != ROOT / ".venv":
        os.execv(str(venv_python), [str(venv_python), __file__, *sys.argv[1:]])
    port = int(os.environ.get("PORT", "8787"))
    if os.environ.get(SECRET_ENV, "").strip():
        print("signature verification on", flush=True)
    else:
        print("signature verification off", flush=True)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"listening http://127.0.0.1:{port}/metric-alert", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
