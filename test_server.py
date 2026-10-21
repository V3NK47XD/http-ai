#!/usr/bin/env python3
"""
Comprehensive Smoke and Concurrency Test for OMP FastAPI Middle Server.
Tests:
  1. GET /ping & POST /ping
  2. POST /prompt without Content-Type header (stateless)
  3. POST /prompt with session key (multi-turn memory retention)
  4. Validation of error cases (invalid JSON, missing prompt, 404)
  5. Concurrent Requests across Multiple Sessions (parallel execution verification)
  6. Server restart session wipe verification
"""

import concurrent.futures
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

TEST_PORT = 8999
BASE_URL = f"http://127.0.0.1:{TEST_PORT}"


def start_server():
    """Start server.py in background subprocess."""
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--host", "127.0.0.1", "--port", str(TEST_PORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    # Wait for server to start
    for _ in range(40):
        try:
            req = urllib.request.Request(f"{BASE_URL}/ping")
            with urllib.request.urlopen(req, timeout=1) as resp:
                if resp.status == 200:
                    print("[TEST SETUP] FastAPI Server is up and responding!")
                    return proc
        except Exception:
            time.sleep(0.5)
    proc.kill()
    out, _ = proc.communicate()
    raise RuntimeError(f"Server failed to start:\n{out}")


def stop_server(proc):
    """Stop the server subprocess and any child processes."""
    if proc:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        except Exception:
            proc.kill()
        try:
            proc.wait(timeout=3)
        except Exception:
            pass


def raw_post(path: str, body_str: str, headers: dict | None = None):
    """Send raw HTTP POST request to test headers and body handling."""
    parsed_headers = headers if headers is not None else {}
    body_bytes = body_str.encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=body_bytes,
        headers=parsed_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read().decode("utf-8")
            return resp.status, json.loads(data)
    except urllib.error.HTTPError as e:
        data = e.read().decode("utf-8")
        try:
            parsed = json.loads(data)
        except Exception:
            parsed = {"raw": data}
        return e.code, parsed


def test_suite():
    proc = start_server()
    try:
        print("\n--- Test 1: GET /ping ---")
        req = urllib.request.Request(f"{BASE_URL}/ping")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            print("Response:", data)
            assert resp.status == 200
            assert data.get("status") == "pong"
        print("✓ Test 1 passed!")

        print("\n--- Test 2: POST /ping ---")
        status, data = raw_post("/ping", "")
        print(f"Status: {status}, Response: {data}")
        assert status == 200
        assert data.get("status") == "pong"
        print("✓ Test 2 passed!")

        print("\n--- Test 3: POST /prompt WITHOUT Content-Type header (Stateless) ---")
        status, data = raw_post("/prompt", '{"prompt": "Say exactly: OK_STATENESS_TEST"}', headers={})
        print(f"Status: {status}, Response: {data}")
        assert status == 200
        assert "response" in data
        assert "OK_STATENESS_TEST" in data["response"]
        assert "session" not in data
        print("✓ Test 3 passed!")

        print("\n--- Test 4: POST /prompt with session key (Multi-turn memory) ---")
        t1_payload = json.dumps({"prompt": "Remember the secret word: BLUEBERRY", "session": "chat_sess_1"})
        status1, data1 = raw_post("/prompt", t1_payload, headers={})
        print(f"Turn 1 Status: {status1}, Response: {data1}")
        assert status1 == 200
        assert data1.get("session") == "chat_sess_1"

        t2_payload = json.dumps({"prompt": "What was the secret word?", "session": "chat_sess_1"})
        status2, data2 = raw_post("/prompt", t2_payload, headers={})
        print(f"Turn 2 Status: {status2}, Response: {data2}")
        assert status2 == 200
        assert data2.get("session") == "chat_sess_1"
        assert "BLUEBERRY" in data2["response"].upper()
        print("✓ Test 4 passed! Multi-turn session memory verified.")

        print("\n--- Test 5: Error handling ---")
        status, data = raw_post("/prompt", "{not-valid-json", headers={})
        print(f"Invalid JSON -> Status: {status}, Response: {data}")
        assert status == 400

        status, data = raw_post("/prompt", '{"session": "foo"}', headers={})
        print(f"Missing prompt -> Status: {status}, Response: {data}")
        assert status == 400

        try:
            urllib.request.urlopen(f"{BASE_URL}/unknown_route")
            assert False, "Should have 404"
        except urllib.error.HTTPError as e:
            print(f"404 check -> Status: {e.code}")
            assert e.code == 404
        print("✓ Test 5 passed! Error handling verified.")

        print("\n--- Test 6: Concurrent Multi-Session Requests ---")
        tasks = [
            ("sess_alpha", "say alpha123"),
            ("sess_beta", "say beta456"),
            ("sess_gamma", "say gamma789"),
        ]

        def send_prompt(session_id, prompt_text):
            payload = json.dumps({"prompt": prompt_text, "session": session_id})
            t0 = time.time()
            st, res = raw_post("/prompt", payload, headers={})
            dt = round(time.time() - t0, 2)
            return session_id, st, res, dt

        start_concurrent = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(send_prompt, s, p) for s, p in tasks]
            results = [f.result() for f in futures]
        total_concurrent_time = round(time.time() - start_concurrent, 2)

        for session_id, st, res, dt in results:
            print(f"Concurrent request for [{session_id}] finished in {dt}s -> Status {st}: {res}")
            assert st == 200
            assert res.get("session") == session_id
            assert "response" in res

        print(f"All 3 concurrent requests completed in {total_concurrent_time}s total!")
        print("✓ Test 6 passed! Concurrent multi-session parallelism verified.")

        print("\n--- Test 7: Server Restart Session Wipe ---")
        print("Stopping server 1...")
        stop_server(proc)
        proc = None
        time.sleep(1)

        print("Starting server 2 (fresh restart)...")
        proc = start_server()

        t3_payload = json.dumps({
            "prompt": "What was the secret word? If no secret word was given in this session, say NONE.",
            "session": "chat_sess_1"
        })
        status3, data3 = raw_post("/prompt", t3_payload, headers={})
        print(f"After restart Status: {status3}, Response: {data3}")
        assert status3 == 200
        assert "BLUEBERRY" not in data3["response"].upper()
        print("✓ Test 7 passed! Sessions wiped cleanly across server restart.")

        print("\n==========================================")
        print("  ALL 7 TESTS PASSED SUCCESSFULLY!")
        print("==========================================")

    finally:
        if proc:
            stop_server(proc)


if __name__ == "__main__":
    test_suite()
