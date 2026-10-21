# OMP HTTP Middle Server

A lightweight, zero-dependency Python HTTP server that exposes OMP (AI Coding Assistant / LLM Runner) over simple HTTP requests.

---

## Features

- **Standard Library Only**: Built with Python's `http.server.ThreadingHTTPServer` (no `pip install` required).
- **2 Clean Endpoints**:
  - `GET` or `POST` `/ping` for health check.
  - `POST` `/prompt` for handling AI prompts.
- **No Headers Required**: Takes JSON payload directly (no `Content-Type: application/json` required in `curl`).
- **Flexible JSON Payload**:
  - `{"prompt": "..."}`: Pure stateless request.
  - `{"prompt": "...", "session": "..."}`: Multi-turn session memory.
- **Ephemeral Sessions**: All sessions and temporary context files are deleted when the Python server restarts.
- **Thread-safe & Concurrent**: Handles requests concurrently with per-session locking to ensure safe state transitions.

---

## OMP Prerequisites & Setup

Before running the Python middle server, ensure `omp` is installed, authenticated, and accessible in your environment.

### 1. Verify OMP Installation
Ensure the `omp` CLI is installed and in your system's `PATH`:
```bash
omp --version
```
*(If not installed, install OMP via your system package manager or installer, and ensure its directory is added to your `PATH`).*

### 2. Configure Model Credentials
OMP needs access to at least one LLM provider. Configure authentication using one of the following methods:

- **Option A: Interactive Login (Recommended)**
  ```bash
  omp login
  ```
  Follow the terminal prompts to authenticate with your preferred provider (e.g., Google, OpenAI, Anthropic).

- **Option B: Environment Variables**
  Set the API key corresponding to your provider:
  ```bash
  # Windows PowerShell
  $env:GEMINI_API_KEY = "your_key_here"
  # or
  $env:OPENAI_API_KEY = "your_key_here"
  # or
  $env:ANTHROPIC_API_KEY = "your_key_here"

  # Linux / macOS / Bash
  export GEMINI_API_KEY="your_key_here"
  ```

- **Option C: Auth Broker / Auth Gateway**
  If managing credentials centrally or exposing through an HTTP forward proxy:
  ```bash
  omp auth-gateway
  ```

### 3. Verify OMP CLI Works Directly
Test that OMP responds in non-interactive print mode:
```bash
omp -p --no-tools "say hello"
```
If you see a direct response (e.g. `Hello!`), OMP is ready and you can start `server.py`.

---

## Quickstart

### 1. Start Server

```bash
python server.py --port 8000
```

*(Optional flags: `--host 0.0.0.0`, `--port 8000`, or via environment variables `PORT=8000 HOST=0.0.0.0`)*

---

## API & cURL Usage

### 1. Health Check (`/ping`)

```bash
curl http://localhost:8000/ping
```

**Response:**
```json
{"status": "pong"}
```

---

### 2. Stateless Prompt (`/prompt`)

No extra headers needed, just send the JSON body:

```bash
curl -d '{"prompt": "Explain quantum computing in one sentence"}' http://localhost:8000/prompt
```

**Response:**
```json
{
  "response": "Quantum computing uses quantum bits to perform calculations exponentially faster than classical computers for specific problems."
}
```

---

### 3. Multi-Turn Session (`/prompt`)

Add the optional `"session"` key to maintain conversation history across turns:

#### Turn 1:
```bash
curl -d '{"prompt": "My favorite color is emerald green", "session": "chat123"}' http://localhost:8000/prompt
```

**Response:**
```json
{
  "response": "Understood! Emerald green is a vibrant color.",
  "session": "chat123"
}
```

#### Turn 2 (recalls Turn 1):
```bash
curl -d '{"prompt": "What is my favorite color?", "session": "chat123"}' http://localhost:8000/prompt
```

**Response:**
```json
{
  "response": "Your favorite color is emerald green.",
  "session": "chat123"
}
```

---

## Running the Automated Test Suite

```bash
python test_server.py
```
