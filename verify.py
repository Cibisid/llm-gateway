"""End-to-end verification: starts the real server, hits it over real HTTP.

    python verify.py

Everything else in this repo is tested in-process. This starts uvicorn as a
separate process and talks to it over the network, which is the only way to
find out whether it actually works as a *service* rather than as a library.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import httpx

BASE = "http://127.0.0.1:8127"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


def main() -> int:
    from app.config import get_settings

    settings = get_settings()
    key = settings.gateway_api_keys.split(",")[0].strip()
    if not key:
        print("GATEWAY_API_KEYS is not set — cannot verify auth.")
        return 2
    auth = {"Authorization": f"Bearer {key}"}

    print("\nStarting the real server on :8127 ...")
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", "8127", "--log-level", "warning"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    try:
        # Wait for it to come up.
        up = False
        for _ in range(40):
            try:
                if httpx.get(f"{BASE}/healthz", timeout=2).status_code == 200:
                    up = True
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not up:
            print("Server never became reachable.")
            return 1

        print("\n1. HEALTH & DISCOVERY")
        h = httpx.get(f"{BASE}/healthz", timeout=10).json()
        check("healthz responds", h.get("status") == "ok")
        check("providers registered", bool(h.get("available_models")),
              ", ".join(h.get("available_models", [])))
        check("MCP tools discovered", len(h.get("mcp_tools", [])) == 3,
              ", ".join(h.get("mcp_tools", [])))

        print("\n2. SECURITY")
        r = httpx.post(f"{BASE}/v1/chat/completions", timeout=10,
                       json={"model": "claude-haiku-4-5",
                             "messages": [{"role": "user", "content": "hi"}]})
        check("no key -> 401", r.status_code == 401, f"got {r.status_code}")

        r = httpx.post(f"{BASE}/v1/chat/completions", timeout=10,
                       headers={"Authorization": "Bearer wrong-key"},
                       json={"model": "claude-haiku-4-5",
                             "messages": [{"role": "user", "content": "hi"}]})
        check("wrong key -> 401", r.status_code == 401, f"got {r.status_code}")

        r = httpx.post(f"{BASE}/v1/chat/completions", timeout=10, headers=auth,
                       json={"model": "nonexistent-model",
                             "messages": [{"role": "user", "content": "hi"}]})
        check("unknown model -> 404", r.status_code == 404, f"got {r.status_code}")

        r = httpx.post(f"{BASE}/v1/chat/completions", timeout=10, headers=auth,
                       json={"model": "claude-haiku-4-5",
                             "messages": [{"role": "user", "content": "hi"}],
                             "stream": True})
        check("stream rejected -> 400", r.status_code == 400, f"got {r.status_code}")

        print("\n3. COMPLETION  (real model call)")
        r = httpx.post(f"{BASE}/v1/chat/completions", timeout=90, headers=auth,
                       json={"model": "claude-haiku-4-5", "max_tokens": 30,
                             "messages": [{"role": "user",
                                           "content": "Reply with exactly: OK"}]})
        check("returns 200", r.status_code == 200, f"got {r.status_code}")
        if r.status_code == 200:
            b = r.json()
            check("OpenAI response shape", b["object"] == "chat.completion")
            check("answer present", bool(b["choices"][0]["message"]["content"].strip()),
                  repr(b["choices"][0]["message"]["content"].strip()))
            check("usage reported", b["usage"]["total_tokens"] > 0,
                  f"{b['usage']['total_tokens']} tokens")
            check("provider reported", b["gateway"]["provider"] == "anthropic")
            check("cost reported", b["gateway"]["cost_usd"] is not None,
                  f"${b['gateway']['cost_usd']:.8f}")

        print("\n4. COST ROUTING  (alias -> cheapest)")
        r = httpx.post(f"{BASE}/v1/chat/completions", timeout=90, headers=auth,
                       json={"model": "auto-cheap", "max_tokens": 20,
                             "messages": [{"role": "user", "content": "Say: HI"}]})
        if r.status_code == 200:
            b = r.json()
            check("alias resolved to a real model", b["gateway"]["upstream_model"] != "auto-cheap",
                  b["gateway"]["upstream_model"])
        else:
            check("alias request", False, f"got {r.status_code}")

        print("\n5. TOOL CALLING  (real MCP tools + real model)")
        r = httpx.post(f"{BASE}/v1/chat/completions", timeout=180, headers=auth,
                       json={"model": "claude-sonnet-5", "use_mcp_tools": True,
                             "messages": [{"role": "user",
                                           "content": "What is the temperature of pump 3?"}]})
        check("returns 200", r.status_code == 200, f"got {r.status_code}")
        if r.status_code == 200:
            b = r.json()
            tools = [s["tool"] for s in b["gateway"]["tool_steps"]]
            check("a tool actually ran", bool(tools), ", ".join(tools))
            check("answer is grounded in real data", "87.4" in b["choices"][0]["message"]["content"],
                  b["choices"][0]["message"]["content"][:70].replace("\n", " "))

        print("\n6. A2A AGENT CARD")
        r = httpx.get(f"{BASE}/a2a/specialist/.well-known/agent-card.json", timeout=10)
        check("card served", r.status_code == 200)
        if r.status_code == 200:
            card = r.json()
            check("advertises skills", len(card.get("skills", [])) == 2,
                  ", ".join(s["id"] for s in card.get("skills", [])))
            check("honest about streaming", card["capabilities"]["streaming"] is False)

        print("\n7. AGENT ENDPOINT  (plan -> act -> observe)")
        r = httpx.post(f"{BASE}/v1/agent", timeout=300, headers=auth,
                       json={"model": "claude-sonnet-5",
                             "messages": [{"role": "user",
                                           "content": "Is pump 3 too hot, and what does the manual say?"}]})
        check("returns 200", r.status_code == 200, f"got {r.status_code}")
        if r.status_code == 200:
            b = r.json()
            g = b["gateway"]
            check("agent produced a plan", bool(g["plan_steps"]),
                  f"{len(g['plan_steps'])} steps: {g['plan_goal'][:50]}")
            check("plan parsed cleanly", g["plan_malformed"] is False)
            check("tools ran", bool(g["tool_steps"]),
                  ", ".join(s["tool"] for s in g["tool_steps"]))
            check("cited the manual", "PMP-4.2" in b["choices"][0]["message"]["content"])
            check("cost tracked", g["cost_usd"] is not None, f"${g['cost_usd']:.6f}")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    passed = sum(1 for _, ok, _ in results if ok)
    failed = len(results) - passed
    print("\n" + "=" * 62)
    print(f"  {passed} passed, {failed} failed, of {len(results)} checks")
    print("=" * 62 + "\n")
    if failed:
        for name, ok, detail in results:
            if not ok:
                print(f"  FAILED: {name}  {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
