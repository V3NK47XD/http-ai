#!/usr/bin/env python3
"""
FastAPI Middle Server for OMP (AI Coding Assistant / LLM Runner).
Provides high-performance asynchronous request handling with:
  - GET /ping & POST /ping -> Health check
  - POST /prompt           -> Async prompt execution
  - Concurrent multi-session support (parallel execution across sessions)
  - Per-session serialization locks to prevent session file conflicts
  - Zero-header requirements (accepts raw JSON body directly)
  - Ephemeral session management (wiped on server restart)
"""

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn

# Base directory for temporary session storage (wiped on startup and shutdown)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_DIR = os.path.join(BASE_DIR, ".omp_sessions")

# In-memory mapping of user session keys -> OMP internal session IDs
session_map: dict[str, str] = {}
session_locks: dict[str, asyncio.Lock] = {}
locks_guard = asyncio.Lock()


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


def clean_code_fences(text: str) -> str:
    """
    If text is enclosed in markdown code fences (```lang ... ```), cleanly strip the fences.
    """
    trimmed = text.strip()
    if trimmed.startswith("```") and trimmed.endswith("```"):
        first_newline = trimmed.find("\n")
        if first_newline != -1:
            inner = trimmed[first_newline + 1 :]
            if inner.endswith("```"):
                return inner[:-3].strip("\r\n")
    return text


async def get_session_lock(session_key: str) -> asyncio.Lock:
    """Retrieve or create an async Lock for a given session key."""
    async with locks_guard:
        if session_key not in session_locks:
            session_locks[session_key] = asyncio.Lock()
        return session_locks[session_key]


async def run_omp(prompt: str, user_session: str | None = None, timeout: int = 180):
    """
    Execute omp subprocess asynchronously without blocking the event loop.
    Returns (reply_text, omp_session_id).
    """
    cmd = [
        "omp",
        "-p",
        "--no-tools",
        "--mode=json",
        "--system-prompt",
        (
            "You are an expert AI assistant. Provide direct, high-quality, concise responses. "
            "When asked for code, scripts, or files, output only the clean, working code directly. "
            "Do not wrap whole code or file responses in markdown code block fences (no ```). "
            "Do not include conversational chatter, preamble, or explanations unless specifically requested."
        ),
    ]
    if user_session:
        omp_id = session_map.get(user_session)
        if omp_id:
            cmd.extend(["--session-dir", SESSION_DIR, "--resume", omp_id])
        else:
            cmd.extend(["--session-dir", SESSION_DIR])
    else:
        cmd.append("--no-session")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=prompt.encode("utf-8")),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise TimeoutError(f"OMP command timed out after {timeout} seconds.")

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")

    if proc.returncode != 0 and not stdout:
        err_msg = stderr.strip() if stderr else f"Exit code {proc.returncode}"
        raise RuntimeError(f"OMP execution failed: {err_msg}")

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

    if not reply_text:
        reply_text = stdout.strip()

    reply_text = clean_code_fences(reply_text)
    if user_session and discovered_session_id:
        session_map[user_session] = discovered_session_id

    return reply_text, discovered_session_id


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager: ensure fresh session directory on startup and cleanup on shutdown."""
    init_session_dir()
    print("[INFO] Initialized clean session directory.")
    yield
    wipe_session_dir()
    print("[INFO] Wiped session directory on shutdown.")


app = FastAPI(
    title="OMP HTTP Middle Server",
    description="High-performance asynchronous middle server exposing OMP over HTTP",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS middleware for open client/browser accessibility
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/ping")
@app.post("/ping")
async def ping():
    """Health check endpoint."""
    return {"status": "pong"}


@app.post("/prompt")
async def handle_prompt(request: Request):
    """
    Handle prompt submissions.
    Takes JSON body directly without enforcing Content-Type header:
      {
        "prompt": "your prompt here",   (required)
        "session": "session_id"         (optional)
      }
    """
    try:
        raw_body = await request.body()
    except Exception as e:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": f"Failed to read request body: {e}"},
        )

    if not raw_body or not raw_body.strip():
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "Request body must not be empty"},
        )

    decoded_body = raw_body.decode("utf-8", errors="replace").strip()
    user_session = None
    raw_mode = "text/plain" in request.headers.get("accept", "")

    try:
        payload = json.loads(decoded_body)
        if isinstance(payload, dict):
            extracted_prompt = (
                payload.get("prompt")
                or payload.get("message")
                or payload.get("text")
            )
            if isinstance(extracted_prompt, str) and extracted_prompt.strip():
                prompt = extracted_prompt.strip()
            else:
                prompt = decoded_body

            sess_val = payload.get("session")
            if sess_val is not None:
                user_session = str(sess_val).strip() or None

            if payload.get("raw") is True:
                raw_mode = True
        elif isinstance(payload, str) and payload.strip():
            prompt = payload.strip()
        else:
            prompt = decoded_body
    except json.JSONDecodeError:
        # Handle invalid JSON gracefully: use entire body as prompt
        prompt = decoded_body
    start_time = time.time()
    try:
        if user_session:
            lock = await get_session_lock(user_session)
            async with lock:
                reply, _ = await run_omp(prompt, user_session=user_session)
        else:
            # Completely unconstrained parallel execution for stateless requests
            reply, _ = await run_omp(prompt, user_session=None)

        duration = round(time.time() - start_time, 2)
        print(
            f"[INFO] Prompt completed in {duration}s"
            + (f" [session={user_session}]" if user_session else " [stateless]")
        )

        if raw_mode:
            return Response(content=reply, media_type="text/plain; charset=utf-8")
        response_data = {"response": reply}
        if user_session:
            response_data["session"] = user_session

        return JSONResponse(status_code=status.HTTP_200_OK, content=response_data)

    except TimeoutError as te:
        return JSONResponse(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            content={"error": str(te)},
        )
    except Exception as e:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": str(e)},
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OMP FastAPI Middle Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on (default: 8000)")
    args = parser.parse_args()

    port = int(os.environ.get("PORT", args.port))
    host = os.environ.get("HOST", args.host)

    uvicorn.run(app, host=host, port=port, log_level="info")
