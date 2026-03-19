"""Minimal local API for volky-bot dashboard actions.

Runs on port 8770. Provides:
- GET  /status   — bot + AI model status
- POST /restart  — restart the trading bot
- POST /train    — start PatchTST training
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread

BASE = Path(__file__).parent.parent
SESSION_LOG = BASE / "papertrade" / "scalp_live_session.log"
LOCK_FILE = BASE / "papertrade" / "scalp_live.lock"
VENV_PYTHON = Path.home() / "Desktop/2026/venv-chronos311/bin/python"
BOT_SCRIPT = BASE / "scripts" / "scalp_live_testnet.py"
TRAIN_SCRIPT = BASE / "train_patchtst.command"

PORT = 8770


def _get_ai_status() -> dict:
    """Parse last AI_MODELS_LOADED from session log."""
    try:
        # Read last 50KB of log (avoid loading huge file)
        with open(SESSION_LOG, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 50000))
            tail = f.read().decode("utf-8", errors="replace")
        for line in reversed(tail.split("\n")):
            if "AI_MODELS_LOADED" in line:
                import re
                m = re.search(r"\{.*\}", line)
                if m:
                    return json.loads(m.group().replace("'", '"').replace("True", "true").replace("False", "false"))
    except Exception:
        pass
    return {}


def _is_bot_running() -> bool:
    try:
        result = subprocess.run(["pgrep", "-f", "scalp_live_testnet"], capture_output=True)
        return result.returncode == 0
    except Exception:
        return False


def _restart_bot() -> str:
    # Kill existing
    subprocess.run(["pkill", "-f", "scalp_live_testnet"], capture_output=True)
    time.sleep(2)
    # Clean lock
    LOCK_FILE.unlink(missing_ok=True)
    # Start new
    python = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
    proc = subprocess.Popen(
        [python, str(BOT_SCRIPT)],
        cwd=str(BASE),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return f"started (PID {proc.pid})"


def _start_training() -> str:
    python = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
    main_dir = Path.home() / "Desktop/2026"
    script = main_dir / "scripts" / "train_patchtst_15m.py"
    if not script.exists():
        return "error: training script not found"
    proc = subprocess.Popen(
        [python, str(script), "--timeframe", "15m"],
        cwd=str(main_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return f"training started (PID {proc.pid})"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/status"):
            ai = _get_ai_status()
            body = json.dumps({
                "bot_running": _is_bot_running(),
                "ai_models": ai,
                "timestamp": int(time.time()),
            })
            self._respond(200, body)
        else:
            self._respond(404, '{"error":"not found"}')

    def do_POST(self):
        if self.path.startswith("/restart"):
            result = _restart_bot()
            self._respond(200, json.dumps({"result": result}))
        elif self.path.startswith("/train"):
            result = _start_training()
            self._respond(200, json.dumps({"result": result}))
        else:
            self._respond(404, '{"error":"not found"}')

    def do_OPTIONS(self):
        self._respond(200, "")

    def _respond(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, format, *args):
        pass  # suppress logs


def main():
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[dash_api] running on http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
