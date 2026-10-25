#!/usr/bin/env python3
"""MCP server (stdio, JSON-RPC 2.0) που εκθέτει τις εντολές του fek.py ως εργαλεία.

Χωρίς εξαρτήσεις πέρα από όσες χρειάζεται το fek.py (pymupdf ή pypdf).
Κάθε κλήση εργαλείου εκτελεί την αντίστοιχη υποεντολή του fek.py και επιστρέφει την έξοδό της.
"""
import io
import json
import os
import re
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SERVER = {"name": "nomos-fek", "version": "1.0.0"}

S = {"type": "string"}
I = {"type": "integer"}
REF_ACT = {"type": "string", "description": "Πράξη, π.χ. «4624/2019» ή «π.δ. 80/2016»."}
REF_ANY = {"type": "string", "description": "Πράξη (4624/2019, «π.δ. 80/2016») ή ετικέτα ΦΕΚ («Α 91/2023»)."}
TYPE = {"type": "string", "description": "Είδος πράξης: νόμος (προεπιλογή), πδ, πνπ, αν, βδ, νδ, νπδ ή all."}

TOOLS = [
    {
        "name": "fek_find",
        "description": "Εντοπίζει σε ποιο ΦΕΚ δημοσιεύτηκε μια πράξη. Επιστρέφει ΦΕΚ, ημερομηνία, τίτλο και σύνδεσμο PDF.",
        "inputSchema": {"type": "object", "properties": {"ref": REF_ANY, "type": TYPE}, "required": ["ref"]},
        "argv": lambda a: ["find", a["ref"], *opt("--type", a.get("type"))],
    },
    {
        "name": "fek_text",
        "description": ("Κείμενο ΦΕΚ όπως δημοσιεύτηκε (όχι κωδικοποιημένο). Με article επιστρέφει μόνο το άρθρο "
                        "και τις σελίδες του· με grep αποσπάσματα με τη φράση (χωρίς τόνους/κεφαλαία). "
                        "Χρησιμοποίησε article ή grep, όχι και τα δύο."),
        "inputSchema": {"type": "object", "properties": {
            "ref": REF_ANY, "type": TYPE,
            "article": {"type": "string", "description": "Άρθρο, π.χ. 11, 84Α, πρώτο."},
            "grep": {"type": "string", "description": "Φράση προς αναζήτηση μέσα στο ΦΕΚ."},
            "limit": {"type": "integer", "description": "Μέγιστα αποσπάσματα για grep (προεπιλογή 15)."},
            "max_chars": {"type": "integer", "description": "Όριο χαρακτήρων (προεπιλογή 9000, 0 = χωρίς όριο)."},
        }, "required": ["ref"]},
        "argv": lambda a: ["text", a["ref"], *opt("--type", a.get("type")), *opt("--article", a.get("article")),
                           *opt("--grep", a.get("grep")), *opt("--limit", a.get("limit")),
                           *opt("--max-chars", a.get("max_chars"))],
    },
    {
        "name": "fek_amendments",
        "description": ("Σαρώνει όλα τα μεταγενέστερα ΦΕΚ που αναφέρουν την πράξη και εντοπίζει πιθανές "
                        "τροποποιήσεις/καταργήσεις (σελίδα, ρήμα, άρθρα, σύνδεσμος PDF). Μόνο για πράξεις, όχι "
                        "ετικέτες ΦΕΚ. Πρώτη εκτέλεση ανά πράξη ~30 s, μετά ~5 s (cache). Κάθε εύρημα θέλει "
                        "επιβεβαίωση με fek_text."),
        "inputSchema": {"type": "object", "properties": {
            "ref": REF_ACT, "type": TYPE,
            "article": {"type": "string", "description": "Μόνο ευρήματα για αυτό το άρθρο."},
            "since": {"type": "string", "description": "Από ΕΕΕΕ-ΜΜ-ΗΗ ή ΕΕΕΕ (προεπιλογή: ημερομηνία ΦΕΚ της πράξης)."},
            "until": {"type": "string", "description": "Έως ΕΕΕΕ-ΜΜ-ΗΗ ή ΕΕΕΕ (προεπιλογή: σήμερα)."},
            "issue": {"type": "string", "description": "Τεύχη: Α (προεπιλογή), «Α,Δ» ή all."},
            "max_docs": {"type": "integer", "description": "Μέγιστο πλήθος ΦΕΚ (προεπιλογή 400)."},
        }, "required": ["ref"]},
        "argv": lambda a: ["amendments", a["ref"], *opt("--type", a.get("type")),
                           *opt("--article", a.get("article")), *opt("--since", a.get("since")),
                           *opt("--until", a.get("until")), *opt("--issue", a.get("issue")),
                           *opt("--max-docs", a.get("max_docs"))],
    },
    {
        "name": "fek_search",
        "description": ("Αναζήτηση πλήρους κειμένου στα ΦΕΚ (π.χ. ΥΑ/ΚΥΑ στο τεύχος Β). Λειτουργεί καλύτερα με "
                        "«N/ΕΕΕΕ» ή μεμονωμένους όρους."),
        "inputSchema": {"type": "object", "properties": {
            "text": {"type": "string", "description": "Φράση προς αναζήτηση."},
            "issue": {"type": "string", "description": "Τεύχη, π.χ. Β ή «Α,Β» (προεπιλογή: όλα)."},
            "year": I,
            "date_from": {"type": "string", "description": "ΕΕΕΕ-ΜΜ-ΗΗ"},
            "date_to": {"type": "string", "description": "ΕΕΕΕ-ΜΜ-ΗΗ"},
            "limit": {"type": "integer", "description": "Μέγιστα αποτελέσματα (προεπιλογή 30)."},
        }, "required": ["text"]},
        "argv": lambda a: ["search", a["text"], *opt("--issue", a.get("issue")), *opt("--year", a.get("year")),
                           *opt("--from", a.get("date_from")), *opt("--to", a.get("date_to")),
                           *opt("--limit", a.get("limit"))],
    },
    {
        "name": "fek_crosscheck",
        "description": ("Διασταύρωση με το κωδικοποιημένο κείμενο νόμου στο taxheaven.gr (δευτερογενής πηγή, μόνο "
                        "για διασταύρωση). Μόνο για νόμους."),
        "inputSchema": {"type": "object", "properties": {
            "ref": {"type": "string", "description": "Νόμος, π.χ. 4624/2019."},
            "article": {"type": "string", "description": "Άρθρο, π.χ. 11 ή 84Α."},
            "max_chars": {"type": "integer", "description": "Όριο χαρακτήρων (προεπιλογή 9000, 0 = χωρίς όριο)."},
        }, "required": ["ref"]},
        "argv": lambda a: ["crosscheck", a["ref"], *opt("--article", a.get("article")),
                           *opt("--max-chars", a.get("max_chars"))],
    },
]
BY_NAME = {t["name"]: t for t in TOOLS}

PROGRESS = re.compile(r"^\s+\d+/\d+ ΦΕΚ")


def opt(flag, val):
    if val is None or val == "":
        return []
    return [flag, str(val)]


def run_tool(name, args):
    import fek
    t = BY_NAME.get(name)
    if not t:
        return f"Άγνωστο εργαλείο: {name}", True
    try:
        argv = t["argv"](args or {})
    except KeyError as e:
        return f"Λείπει η παράμετρος {e}", True
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with redirect_stdout(out), redirect_stderr(err):
        try:
            fek.main(argv)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
            if not isinstance(e.code, int) and e.code is not None:
                print(e.code, file=sys.stderr)
        except Exception:
            code = 1
            traceback.print_exc()
    notes = [l for l in err.getvalue().splitlines() if l.strip() and not PROGRESS.match(l)]
    text = out.getvalue().rstrip()
    if code:
        return "\n".join(notes + ([text] if text else [])) or f"Αποτυχία (κωδικός {code})", True
    if notes:
        text = "\n".join(notes) + "\n\n" + text
    return text, False


def handle(msg):
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        pv = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
        return {"protocolVersion": pv, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER,
                "instructions": "Εργαλεία για τα ΦΕΚ του Εθνικού Τυπογραφείου (search.et.gr). Απάντα στα ελληνικά."}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{k: v for k, v in t.items() if k != "argv"} for t in TOOLS]}
    if method == "tools/call":
        p = msg.get("params") or {}
        text, err = run_tool(p.get("name"), p.get("arguments"))
        return {"content": [{"type": "text", "text": text}], "isError": err}
    if mid is None:
        return None
    raise LookupError(method)


def serve():
    # Το πραγματικό stdout κρατιέται μόνο για JSON-RPC· το fd 1 δείχνει πλέον στο stderr,
    # ώστε ό,τι γράψουν θυγατρικές διεργασίες να μη χαλάσει το πρωτόκολλο.
    proto = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", newline="\n")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            if msg.get("id") is None:
                continue
            try:
                resp = {"jsonrpc": "2.0", "id": msg["id"], "result": handle(msg)}
            except LookupError as e:
                resp = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": f"Method not found: {e}"}}
            except Exception as e:
                resp = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(e)}}
        proto.write(json.dumps(resp, ensure_ascii=False) + "\n")
        proto.flush()


if __name__ == "__main__":
    serve()
