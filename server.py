#!/usr/bin/env python3
"""
HTTP Middle Server for OMP (AI Coding Assistant / LLM Runner).
Provides 2 simple HTTP endpoints:
  - GET  /ping   -> Health check
  - POST /prompt -> Sends prompt to OMP backend
"""

import argparse
import atexit
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Base directory for temporary session storage (wiped on startup and exit)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_DIR = os.path.join(BASE_DIR, ".omp_sessions")

# In-memory mapping of user session keys -> OMP internal session IDs
session_map = {}
session_locks = {}
map_lock = threading.Lock()


def wipe_session_dir():
    """Wipe any temporary session data on disk."""
    if os.path.exists(SESSION_DIR):
        try:
            shutil.rmtree(SESSION_DIR, ignore_errors=True)
        except Exception as e:
            print(f"[WARN] Error wiping session directory: {e}", file=sys.stderr)


def init_session_dir():
    """Ensure clean empty session directory exists."""
    wipe_session_dir()
    os.makedirs(SESSION_DIR, exist_ok=True)


def get_session_lock(session_key: str) -> threading.Lock:
    """Retrieve or create a thread lock for a given session key."""
    with map_lock:
        if session_key not in session_locks:
            session_locks[session_key] = threading.Lock()
        return session_locks[session_key]


def run_omp(prompt: str, user_session: str | None = None, timeout: int = 180):
    """
    Execute omp subprocess with the given prompt.
    Returns (reply_text, omp_session_id).
    """
    cmd = [
        "omp",
        "-p",
        "--no-tools",
        "--mode=json",
        "--system-prompt",
        "You are a helpful AI chat assistant. Answer the user prompt directly in plain text.",
    ]

    if user_session:
        with map_lock:
            omp_id = session_map.get(user_session)

        if omp_id:
            cmd.extend(["--session-dir", SESSION_DIR, "--resume", omp_id])
        else:
            cmd.extend(["--session-dir", SESSION_DIR])
    else:
        cmd.append("--no-session")

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )

    try:
        stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise TimeoutError(f"OMP command timed out after {timeout} seconds.")

    if proc.returncode != 0 and not stdout:
        err_msg = stderr.strip() if stderr else f"Exit code {proc.returncode}"
        raise RuntimeError(f"OMP execution failed: {err_msg}")

    # Parse JSONL events from stdout
    discovered_session_id = None
    reply_text = ""
    fallback_deltas = []

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type")
        if event_type == "session":
            discovered_session_id = event.get("id")
        elif event_type == "turn_end":
            msg = event.get("message", {})
            if msg.get("role") == "assistant":
                content = msg.get("content", [])
                text_pieces = [c["text"] for c in content if c.get("type") == "text"]
                if text_pieces:
                    reply_text = "".join(text_pieces)
        elif event_type == "message_end":
            msg = event.get("message", {})
            if msg.get("role") == "assistant" and not reply_text:
                content = msg.get("content", [])
                text_pieces = [c["text"] for c in content if c.get("type") == "text"]
                if text_pieces:
                    reply_text = "".join(text_pieces)
        elif event_type == "message_update":
            asst_event = event.get("assistantMessageEvent", {})
            if asst_event.get("type") == "text_delta":
                fallback_deltas.append(asst_event.get("delta", ""))

    if not reply_text and fallback_deltas:
        reply_text = "".join(fallback_deltas)

    # Fallback to raw stdout if JSON parsing found no text
    if not reply_text:
        reply_text = stdout.strip()

    if user_session and discovered_session_id:
        with map_lock:
            session_map[user_session] = discovered_session_id

    return reply_text, discovered_session_id


class OMPRequestHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler supporting /ping and /prompt."""

    server_version = "OMP-HTTP/1.0"

    def _send_json(self, status_code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        """Handle pre-flight CORS requests."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        """Handle GET requests."""
        if self.path == "/ping" or self.path.startswith("/ping?"):
            self._send_json(200, {"status": "pong"})
        else:
            self._send_json(404, {"error": f"Endpoint not found: {self.path}"})

    def do_POST(self):
        """Handle POST requests."""
        if self.path == "/ping" or self.path.startswith("/ping?"):
            self._send_json(200, {"status": "pong"})
            return

        if self.path != "/prompt" and not self.path.startswith("/prompt?"):
            self._send_json(404, {"error": f"Endpoint not found: {self.path}"})
            return

        # Read request body without enforcing Content-Type header
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            content_length = 0

        if content_length > 0:
            raw_body = self.rfile.read(content_length).decode("utf-8", errors="replace")
        else:
            # Handle chunked or non-length bodies if present
            raw_body = self.rfile.read().decode("utf-8", errors="replace")

        if not raw_body.strip():
            self._send_json(400, {"error": "Request body must not be empty"})
            return

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": f"Invalid JSON body: {e.msg}"})
            return

        if not isinstance(payload, dict):
            self._send_json(400, {"error": "JSON body must be an object"})
            return

        prompt = payload.get("prompt")
        if not prompt or not isinstance(prompt, str) or not prompt.strip():
            self._send_json(400, {"error": "Missing or empty required field 'prompt'"})
            return

        user_session = payload.get("session")
        if user_session is not None:
            user_session = str(user_session).strip()
            if not user_session:
                user_session = None

        start_time = time.time()
        try:
            if user_session:
                lock = get_session_lock(user_session)
                with lock:
                    reply, _ = run_omp(prompt, user_session=user_session)
            else:
                reply, _ = run_omp(prompt, user_session=None)

            duration = round(time.time() - start_time, 2)
            print(
                f"[INFO] Prompt handled in {duration}s"
                + (f" [session={user_session}]" if user_session else " [stateless]")
            )

            response_data = {"response": reply}
            if user_session:
                response_data["session"] = user_session

            self._send_json(200, response_data)

        except TimeoutError as te:
            print(f"[ERROR] Timeout: {te}", file=sys.stderr)
            self._send_json(504, {"error": str(te)})
        except Exception as e:
            print(f"[ERROR] Execution failed: {e}", file=sys.stderr)
            self._send_json(500, {"error": str(e)})

    def log_message(self, format, *args):
        """Custom HTTP request logger."""
        sys.stdout.write(f"[{self.log_date_time_string()}] {args[0]} {args[1]} -> {args[2]}\n")
        sys.stdout.flush()


def run_server(host: str = "0.0.0.0", port: int = 8000):
    """Start the HTTP server."""
    init_session_dir()

    # Clean up session files on graceful shutdown
    atexit.register(wipe_session_dir)

    def sig_handler(signum, frame):
        print("\n[INFO] Shutting down server and wiping sessions...")
        wipe_session_dir()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    server_address = (host, port)
    httpd = ThreadingHTTPServer(server_address, OMPRequestHandler)
    print(f"==================================================")
    print(f"  OMP HTTP Middle Server running on http://{host}:{port}")
    print(f"  - GET  http://{host}:{port}/ping")
    print(f"  - POST http://{host}:{port}/prompt")
    print(f"  Sessions are ephemeral (wiped on server restart)")
    print(f"==================================================")
    sys.stdout.flush()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        wipe_session_dir()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OMP HTTP Middle Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on (default: 8000)")
    args = parser.parse_args()

    # Allow PORT env var override
    port = int(os.environ.get("PORT", args.port))
    host = os.environ.get("HOST", args.host)

    run_server(host=host, port=port)
