#!/usr/bin/env python3
"""Smoke test του MCP server: python tests/test_mcp_smoke.py (χρειάζεται δίκτυο)."""
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "tools", "fek_mcp.py")], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=ROOT)
    n = 0

    def call(method, params=None, notify=False):
        nonlocal n
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            n += 1
            msg["id"] = n
        p.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        p.stdin.flush()
        if notify:
            return None
        resp = json.loads(p.stdout.readline().decode("utf-8"))
        assert resp.get("id") == n, resp
        assert "result" in resp, resp
        return resp["result"]

    fails = 0

    def check(name, cond, detail=""):
        nonlocal fails
        print(("OK   " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
        fails += not cond

    try:
        r = call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "smoke", "version": "0"}})
        check("initialize", r["serverInfo"]["name"] == "nomos-fek")
        call("notifications/initialized", notify=True)
        names = [t["name"] for t in call("tools/list")["tools"]]
        check("tools/list", names == ["fek_find", "fek_text", "fek_amendments", "fek_search", "fek_crosscheck"],
              str(names))

        def tool(name, **args):
            t = time.time()
            r = call("tools/call", {"name": name, "arguments": args})
            return r["content"][0]["text"], r["isError"], time.time() - t

        text, err, s = tool("fek_find", ref="4624/2019")
        check(f"fek_find ({s:.1f}s)", not err and "137" in text, text[:300])
        text, err, s = tool("fek_text", ref="4624/2019", article="11")
        check(f"fek_text article ({s:.1f}s)", not err and "Λειτουργική" in text, text[:300])
        text, err, s = tool("fek_amendments", ref="4624/2019", article="11")
        check(f"fek_amendments ({s:.1f}s)", not err and "5043/2023" in text and "page=62" in text, text[:500])
        text, err, s = tool("fek_text", ref="Α 91/2023", grep="4624/2019")
        check(f"fek_text grep ({s:.1f}s)", not err and "εμφανίσεις" in text, text[:300])
        text, err, s = tool("fek_search", text="4624/2019", issue="Α", date_from="2026-01-01")
        check(f"fek_search ({s:.1f}s)", not err and "ΦΕΚ" in text, text[:300])
        text, err, s = tool("fek_crosscheck", ref="4624/2019", article="11")
        check(f"fek_crosscheck ({s:.1f}s)", not err and "taxheaven" in text, text[:300])
        text, err, s = tool("fek_text", ref="Α 91/2023", article="1", grep="x")
        check("σφάλμα ορισμάτων → isError", err, text[:300])
        text, err, s = tool("fek_amendments", ref="Α 91/2023")
        check("ετικέτα ΦΕΚ στο amendments → isError", err, text[:300])
        text, err, s = tool("fek_nope")
        check("άγνωστο εργαλείο → isError", err)
        check("server ζωντανός", call("ping") == {})
    finally:
        p.stdin.close()
        p.wait(timeout=30)
    print(f"\n{'ΕΠΙΤΥΧΙΑ' if not fails else f'{fails} ΑΠΟΤΥΧΙΕΣ'}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
