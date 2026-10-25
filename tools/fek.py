#!/usr/bin/env python3
"""fek.py: έρευνα ΦΕΚ μέσω του ανοιχτού API του search.et.gr (Εθνικό Τυπογραφείο).

Υποεντολές
  find N/ΕΕΕΕ [--type πδ]               Ποιο ΦΕΚ δημοσίευσε μια πράξη (νόμο, π.δ., ν.δ. κ.λπ.).
  text REF [--article Α | --grep ΚΕΙΜ]   Κείμενο ΦΕΚ ΟΠΩΣ ΔΗΜΟΣΙΕΥΤΗΚΕ (όχι ενοποιημένο).
  amendments N/ΕΕΕΕ [--article Α]       Μεταγενέστερα ΦΕΚ που φαίνεται να τροποποιούν/καταργούν την πράξη.
  search "ΚΕΙΜΕΝΟ" [--issue Β]          Αναζήτηση πλήρους κειμένου στα ΦΕΚ.
  crosscheck N/ΕΕΕΕ [--article Α]       Κωδικοποιημένο κείμενο νόμου από το taxheaven.gr (μόνο για διασταύρωση).

REF: «4624/2019», «ν. 4624/2019», «π.δ. 80/2016» ή ετικέτα ΦΕΚ («Α 91/2023», «ΦΕΚ Β' 1234/2020», «Α' 91/13.04.2023»).
Όλες οι υποεντολές δέχονται --json. Απαιτεί Python 3.9+ και PyMuPDF (pip install pymupdf) ή pypdf.
Τα κείμενα των ΦΕΚ αποθηκεύονται στο %TEMP%/nomos-fek-cache (αλλαγή με τη μεταβλητή FEK_CACHE).
"""
import argparse
import base64
import bisect
import gzip
import html
import io
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from datetime import date, timedelta

API = "https://searchetv99.azurewebsites.net/api"
BLOB = "https://ia37rg02wpsa01.blob.core.windows.net/fek"
FEK_PAGE = "https://search.et.gr/fek/?fekId={}"
TAXHEAVEN = "https://www.taxheaven.gr/law"
UA = "Mozilla/5.0 (compatible; nomos-fek/1.0)"
API_HEADERS = {"Content-Type": "application/json; charset=utf-8", "Origin": "https://search.et.gr",
               "Referer": "https://search.et.gr/", "User-Agent": UA}
MAX_RESULTS = 1500  # όριο αποτελεσμάτων ανά κλήση του API

ISSUES = {1: "Α", 2: "Β", 3: "Γ", 4: "Δ", 5: "Ν.Π.Δ.Δ.", 6: "Α.Π.Σ.", 7: "ΠΑΡΑΡΤΗΜΑ", 8: "Δ.Ε.Β.Ι.",
          9: "Α.ΕΙ.Δ.", 10: "Α.Σ.Ε.Π.", 11: "ΑΕ-ΕΠΕ", 12: "Δ.Δ.Σ.", 13: "Ο.Π.Κ.", 14: "Υ.Ο.Δ.Δ.", 15: "Α.Α.Π."}
ACT_TYPES = {1: "ν.", 2: "π.δ.", 3: "π.ν.π.", 101: "α.ν.", 102: "β.δ.", 301: "ν.δ.", 501: "ν.π.δ."}
TYPE_KEYS = {"ν": 1, "νομοσ": 1, "νομου": 1, "πδ": 2, "πνπ": 3, "αν": 101, "βδ": 102, "νδ": 301, "νπδ": 501}
LAT_GR = str.maketrans("abgdezhiklmnxoprstyf", "αβγδεζηικλμνξοπρστυφ")
LAT_UP = str.maketrans("ABEZHIKMNOPTXY", "ΑΒΕΖΗΙΚΜΝΟΡΤΧΥ")  # λατινικά κεφαλαία που μοιάζουν με ελληνικά


# ---------------------------------------------------------------- κανονικοποίηση κειμένου

def _build_fold():
    t = {}
    for lo, hi in ((0xB5, 0xB6), (0xC0, 0x250), (0x370, 0x400), (0x1F00, 0x2000)):
        for cp in range(lo, hi):
            c = chr(cp)
            base = "".join(x for x in unicodedata.normalize("NFKD", c) if not unicodedata.combining(x)).lower()
            if len(base) == 1 and base != c:
                t[cp] = base
    for cp in range(0x2000, 0x200C):
        t[cp] = " "
    t.update({0x2206: "δ", 0x2126: "ω", ord("ς"): "σ", 0x37E: ";", 0x387: "·", 0xA0: " ", 0x202F: " ",
              0xAD: "-", 0x2010: "-", 0x2011: "-"})
    for c in "\u2019\u2018\u0384\u1fbd\u1fbf\u1ffd\u1fef\u0374\u02b9\u02bc\u2032`\u00b4":
        t[ord(c)] = "'"
    return t


FOLD = _build_fold()
_MARKS_RX = re.compile("[\u0300-\u036f]")


def _fold_basic(s):
    return _MARKS_RX.sub("", unicodedata.normalize("NFD", s)).replace("ς", "σ")


FOLD_REST = {chr(k): v for k, v in FOLD.items() if _fold_basic(chr(k)) != v}
_REST_RX = re.compile("[" + "".join(re.escape(c) for c in FOLD_REST) + "]")


def _lower(s):
    low = s.lower()
    if len(low) != len(s):
        low = "".join(c.lower() if len(c.lower()) == 1 else c for c in s)
    return low


def fold(s):
    """Πεζά, χωρίς τόνους, ς→σ, ενιαίοι απόστροφοι. Διατηρεί το μήκος."""
    return _lower(s).translate(FOLD)


def fold_text(s):
    """Όπως fold(), ~5x ταχύτερα σε μεγάλα κείμενα (NFD αντί για translate χαρακτήρα-χαρακτήρα)."""
    low = _lower(s)
    if not _MARKS_RX.search(low):
        x = _fold_basic(_REST_RX.sub(lambda m: FOLD_REST[m.group()], low))
        if len(x) == len(s):
            return x
    return low.translate(FOLD)


def key(s):
    k = re.sub(r"[\s.\-']", "", fold(s))
    return k.translate(LAT_GR) if re.fullmatch(r"[a-z]+", k) else k


def _tkey(s):
    return re.sub(r"\W", "", fold(s))


def is_all(s):
    return re.sub(r"\W", "", fold(s)) in ("all", "ολα", "ολεσ", "ολοι")


ISSUE_KEYS = {key(v): k for k, v in ISSUES.items()}
ORDINALS = ("πρωτ", "δευτερ", "τριτ", "τεταρτ", "πεμπτ", "εκτ", "εβδομ", "ογδο", "ενατ", "ενδεκατ", "δωδεκατ",
            "δεκατ", "μον")


def canon_art(s):
    """Κανονική μορφή αριθμού άρθρου: «84Α», «84A», «84 α» → «84α», «πρώτου» → «πρωτο»."""
    k = re.sub(r"[\s.']", "", fold(str(s)))
    m = re.fullmatch(r"(\d+)([a-zα-ω]*)", k)
    if m:
        suf = m.group(2).translate(LAT_GR)
        return m.group(1) + ("" if suf in ("ο", "ου") else suf)
    for o in ORDINALS:
        if k.startswith(o):
            return o + "ο"
    return k


def art_sort_key(a):
    m = re.match(r"(\d+)(.*)", a)
    return (0, int(m.group(1)), m.group(2)) if m else (1, 0, a)


# ---------------------------------------------------------------- δίκτυο / API

def die(msg, code=2):
    print(msg, file=sys.stderr)
    sys.exit(code)


def eprint(*a):
    print(*a, file=sys.stderr, flush=True)


def _request(url, data=None, headers=None, timeout=60, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers or {"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise
            last = e
        except Exception as e:  # σφάλματα δικτύου/χρονικού ορίου: νέα προσπάθεια
            last = e
        time.sleep(1.5 * (i + 1))
    raise last


def http_get(url, data=None, headers=None, timeout=60, tries=3):
    return _request(url, data, headers, timeout, tries)[2]


def download(url, chunk=4 << 20, conns=8):
    """Λήψη PDF. Τα μεγάλα (έως ~250 MB) σε παράλληλα τμήματα HTTP Range: ~2 MB/s ανά σύνδεση στο blob."""
    hdr = {"User-Agent": UA}
    st, h, body = _request(url, headers={**hdr, "Range": f"bytes=0-{chunk - 1}"}, timeout=180)
    m = re.fullmatch(r"bytes 0-\d+/(\d+)", (h.get("Content-Range") or "").strip()) if st == 206 else None
    if not m or int(m.group(1)) <= len(body):
        return body
    total = int(m.group(1))
    spans = [(a, min(a + chunk, total) - 1) for a in range(len(body), total, chunk)]

    def part(ab):
        s, _, b = _request(url, headers={**hdr, "Range": f"bytes={ab[0]}-{ab[1]}"}, timeout=180)
        if s != 206 or len(b) != ab[1] - ab[0] + 1:
            raise IOError(f"ελλιπές τμήμα {ab[0]}-{ab[1]} του {url}")
        return b

    with ThreadPoolExecutor(min(conns, len(spans))) as ex:
        return body + b"".join(ex.map(part, spans))


def api(path, body):
    d = json.loads(http_get(API + path, json.dumps(body).encode("utf-8"), API_HEADERS, timeout=120)).get("data")
    if isinstance(d, str):
        d = json.loads(d) if d.strip() else []
    return [r for r in d or [] if isinstance(r, dict) and r.get("search_ID")]


def api_date(s):
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s or "")
    return date(int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else None


def make_fek(issue, num, year, d=None, fid=None, pages=0, title=""):
    name = ISSUES.get(issue, str(issue))
    when = f"{d:%d.%m.%Y}" if d else str(year)
    return {"label": f"{name}{chr(39) if len(name) == 1 else ''} {num}/{when}", "issue": issue, "number": num,
            "year": year, "date": d.isoformat() if d else None, "id": fid, "pages": pages, "title": title,
            "pdf": f"{BLOB}/{issue:02d}/{year}/{year}{issue:02d}{num:05d}.pdf",
            "url": FEK_PAGE.format(fid) if fid else None}


def fek_info(r):
    d = api_date(r.get("search_IssueDate")) or api_date(r.get("search_PublicationDate"))
    if d:
        year = d.year
    else:
        y = re.search(r"(\d{4})\s*$", r.get("search_PrimaryLabel") or "")
        year = int(y.group(1)) if y else 0
    return make_fek(int(r.get("search_IssueGroupID") or 0), int(r.get("search_DocumentNumber") or 0), year, d,
                    r.get("search_ID"), int(r.get("search_Pages") or 0), (r.get("search_Description") or "").strip())


def find_act(cat, num, year):
    rows = api("/searchlegislation", {"legislationCatalogues": str(cat or ""), "legislationNumber": str(num),
                                      "selectYear": [str(year)]})
    seen, out = set(), []
    for f in sorted((fek_info(r) for r in rows), key=lambda f: f["date"] or ""):
        if f["id"] not in seen:
            seen.add(f["id"])
            out.append(f)
    return out


def find_typed(cat, num, year):
    """→ [(τύπος, ΦΕΚ)]. Με cat="" ελέγχει όλους τους τύπους πράξεων."""
    if cat:
        return [(cat, f) for f in find_act(cat, num, year)]
    with ThreadPoolExecutor(len(ACT_TYPES)) as ex:
        res = list(ex.map(lambda c: [(c, f) for f in find_act(c, num, year)], ACT_TYPES))
    return [x for r in res for x in r]


def simple_search(text, issues, d1=None, d2=None, years=None, number=""):
    return api("/simplesearch", {"selectYear": [str(y) for y in years or []], "selectIssue": [str(i) for i in issues],
                                 "documentNumber": str(number), "searchText": text,
                                 "datePublished": f"{d1:%Y-%m-%d} {d2:%Y-%m-%d}" if d1 else "", "dateReleased": ""})


def resolve_fek(issue, num, year):
    for r in simple_search("", [issue], years=[year], number=num):
        if int(r.get("search_DocumentNumber") or -1) == num and int(r.get("search_IssueGroupID") or -1) == issue:
            return fek_info(r)
    return None


def _search_split(text, issues, d1, d2):
    res = simple_search(text, issues, d1, d2)
    if len(res) < MAX_RESULTS or d1 >= d2:
        return res, len(res) >= MAX_RESULTS
    mid = d1 + (d2 - d1) // 2
    a, ca = _search_split(text, issues, d1, mid)
    b, cb = _search_split(text, issues, mid + timedelta(days=1), d2)
    return a + b, ca or cb


def search_range(text, issues, since, until):
    """Αναζήτηση ανά έτος (παράλληλα)· διασπά διαστήματα που φτάνουν το όριο του API."""
    spans = [(max(since, date(y, 1, 1)), min(until, date(y, 12, 31))) for y in range(since.year, until.year + 1)]
    rows, capped = {}, False
    with ThreadPoolExecutor(max(1, min(6, len(spans)))) as ex:
        for res, cap in ex.map(lambda s: _search_split(text, issues, *s), spans):
            capped |= cap
            for r in res:
                rows[r["search_ID"]] = r
    return list(rows.values()), capped


# ---------------------------------------------------------------- αναφορές (REF)

def parse_ref(s):
    """→ ("act", τύπος|None, αριθμός, έτος) ή ("fek", τεύχος, αριθμός, έτος)."""
    f = re.sub(r"\s+", " ", fold(s).replace("'", " ")).strip()
    f = re.sub(r"^(?:φεκ|fek|τευχοσ)\b\.?\s*", "", f)
    m = re.fullmatch(r"(\d{1,5})\s*/\s*([^\d/]+?)\s*/\s*(.+)", f)
    if m:
        num, pre, tail = m.groups()
    else:
        m = re.fullmatch(r"(\D*?)\s*(\d{1,5})\s*/\s*(.+)", f)
        if not m:
            die(f"Δεν αναγνωρίστηκε η αναφορά «{s}». Παραδείγματα: 4624/2019, π.δ. 80/2016, «Α 91/2023».")
        pre, num, tail = m.groups()
    y = re.match(r"(?:\d{1,2}[./-]\d{1,2}[./-])?(\d{4}|\d{2})(?!\d)", tail)
    if not y:
        die(f"Λείπει το έτος στην αναφορά «{s}».")
    year = int(y.group(1))
    if year < 100:
        year += 2000 if year <= date.today().year % 100 else 1900
    k = key(pre)
    if not k:
        return ("act", None, int(num), year)
    if k in TYPE_KEYS:
        return ("act", TYPE_KEYS[k], int(num), year)
    if k in ISSUE_KEYS:
        return ("fek", ISSUE_KEYS[k], int(num), year)
    die(f"Άγνωστο πρόθεμα «{pre.strip()}» στην αναφορά «{s}».")


def parse_type(s):
    if not s:
        return None
    if is_all(s):
        return ""
    k = key(s)
    if k not in TYPE_KEYS:
        die(f"Άγνωστος τύπος πράξης «{s}». Επιτρέπονται: νόμος, πδ, πνπ, αν, βδ, νδ, νπδ, all.")
    return TYPE_KEYS[k]


def parse_issues(s, default=("1",)):
    if not s:
        return list(default)
    if is_all(s):
        return []
    out = []
    for p in s.split(","):
        k = key(p)
        if k.isdigit():
            out.append(k)
        elif k in ISSUE_KEYS:
            out.append(str(ISSUE_KEYS[k]))
        else:
            die(f"Άγνωστο τεύχος «{p}». Παραδείγματα: Α, Β, Δ, all.")
    return out


def parse_day(s, end=False):
    s = s.strip()
    if re.fullmatch(r"\d{4}", s):
        return date(int(s), 12, 31) if end else date(int(s), 1, 1)
    try:
        return date.fromisoformat(s)
    except ValueError:
        die(f"Μη έγκυρη ημερομηνία «{s}» (μορφή ΕΕΕΕ-ΜΜ-ΗΗ ή ΕΕΕΕ).")


def lookup_act(t, typ, num, year):
    """→ ([(τύπος, ΦΕΚ)], σημείωση). Αν δεν δοθεί τύπος και δεν βρεθεί νόμος, ελέγχει όλους τους τύπους."""
    cat = typ if typ is not None else (t or 1)
    hits = find_typed(cat, num, year)
    if not hits and typ is None and t is None:
        hits = find_typed("", num, year)
        if hits:
            return hits, f"Δεν βρέθηκε νόμος {num}/{year}· βρέθηκε άλλος τύπος πράξης."
    return hits, None


def resolve(ref, typ=None):
    """→ (act|None, fek). Τερματίζει αν δεν βρεθεί."""
    kind, t, num, year = parse_ref(ref)
    if kind == "fek":
        return None, resolve_fek(t, num, year) or make_fek(t, num, year)
    hits, note = lookup_act(t, typ, num, year)
    if not hits:
        die(f"Δεν βρέθηκε πράξη {num}/{year} στο search.et.gr. Δοκίμασε: find {num}/{year} --type all", 1)
    types = sorted({c for c, _ in hits})
    if len(types) > 1:
        die(f"Ασαφές: υπάρχουν {', '.join(f'{ACT_TYPES[c]} {num}/{year}' for c in types)}. Όρισε --type.")
    if note:
        eprint(note)
    cat, f = hits[0]
    return {"type": cat, "number": num, "year": year, "name": f"{ACT_TYPES[cat]} {num}/{year}"}, f


# ---------------------------------------------------------------- κείμενο ΦΕΚ (PDF + cache)

def cache_path(f):
    d = os.environ.get("FEK_CACHE") or os.path.join(tempfile.gettempdir(), "nomos-fek-cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{f['year']}{f['issue']:02d}{f['number']:05d}.txt.gz")


def read_cache(f):
    p = cache_path(f)
    if not os.path.exists(p):
        return None
    try:
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            return tidy(fh.read())
    except (OSError, EOFError, UnicodeDecodeError):
        return None


def write_cache(f, text):
    p = cache_path(f)
    tmp = f"{p}.{os.getpid()}.tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, p)


_TRAIL_WS_RX = re.compile(r"[^\S\n\f]+(?=[\n\f]|$)")


def tidy(text):
    """Χωρίς κενά στο τέλος των γραμμών (λιγότερες συγχωνεύσεις στο Doc)."""
    return _TRAIL_WS_RX.sub("", text)


def pdf_backend():
    """PyMuPDF (προτιμάται) ή pypdf· αλλιώς τερματισμός με οδηγία εγκατάστασης."""
    try:
        import pymupdf as fitz
    except ImportError:
        try:
            import fitz
        except ImportError:
            fitz = None
    if fitz is not None and hasattr(fitz, "open"):
        return fitz
    try:
        import pypdf
    except ImportError:
        die("Χρειάζεται PyMuPDF για την ανάγνωση PDF: pip install pymupdf (ή pypdf)")
    return pypdf


def pdf_to_text(data):
    lib = pdf_backend()
    if lib.__name__ == "pypdf":
        return tidy("\f".join((p.extract_text() or "") for p in lib.PdfReader(io.BytesIO(data)).pages))
    with lib.open(stream=data, filetype="pdf") as doc:
        return tidy("\f".join(p.get_text() for p in doc))


def fek_text(f):
    t = read_cache(f)
    if t is None:
        try:
            data = download(f["pdf"])
        except urllib.error.HTTPError as e:
            die(f"Δεν βρέθηκε το PDF του ΦΕΚ {f['label']} ({e.code}): {f['pdf']}")
        t = pdf_to_text(data)
        write_cache(f, t)
    return t


_TARGETS = {}


def _analyze(f, cat, num, year):
    """Ένα ΦΕΚ: κείμενο (cache ή λήψη PDF) και εντοπισμός τροποποιήσεων. Εκτελείται σε ξεχωριστή διεργασία."""
    res = {"fek": f, "cached": False, "error": None, "scanned": False, "groups": []}
    text = read_cache(f)
    if text is not None:
        res["cached"] = True
    else:
        try:
            text = pdf_to_text(download(f["pdf"]))
            write_cache(f, text)
        except Exception as e:
            res["error"] = str(e) or type(e).__name__
            return res
    if len(text.strip()) < 200:
        res["scanned"] = True
    elif str(num) in text:
        try:
            rx = _TARGETS.get((cat, num, year))
            if rx is None:
                rx = _TARGETS[cat, num, year] = target_rx(cat, num, year)
            doc = Doc(text, f["year"])
            hits = scan(doc, rx)
            if hits:
                res["groups"] = group_hits(doc, f, hits)
        except Exception as e:  # ένα δύσκολο ΦΕΚ δεν σταματά τον έλεγχο των υπολοίπων
            res["error"] = f"ανάλυση: {type(e).__name__}: {e}"
    return res


def analyze_all(feks, cat, num, year, workers):
    """Αποτελέσματα του _analyze για κάθε ΦΕΚ, με τη σειρά που ολοκληρώνονται. Παράλληλα σε διεργασίες,
    όχι νήματα: η ανάλυση είναι CPU-bound και το PyMuPDF δεν είναι thread-safe."""
    left = set(range(len(feks)))
    if workers > 1 and len(feks) > 2:
        try:
            ex = ProcessPoolExecutor(min(workers, len(feks), 61))
        except (OSError, ValueError, NotImplementedError) as e:
            ex = None
            eprint(f"  Χωρίς παράλληλες διεργασίες ({type(e).__name__}): σειριακή επεξεργασία.")
        if ex is not None:
            try:
                futs = {ex.submit(_analyze, f, cat, num, year): i for i, f in enumerate(feks)}
                for fut in as_completed(futs):
                    r = fut.result()
                    left.discard(futs[fut])
                    yield r
            except (BrokenProcessPool, OSError) as e:
                eprint(f"  Οι παράλληλες διεργασίες απέτυχαν ({type(e).__name__}): συνέχεια σειριακά.")
            finally:
                ex.shutdown(wait=True, cancel_futures=True)
    for i in sorted(left):
        yield _analyze(feks[i], cat, num, year)


# ---------------------------------------------------------------- ανάλυση κειμένου ΦΕΚ

def _lat(rx):
    """Δέχεται και λατινικά γράμματα που μοιάζουν με ελληνικά κεφαλαία («NOMOΣ» με λατινικά N, O, M)."""
    look = dict(zip("ΑΒΕΖΗΙΚΜΝΟΡΤΧΥ", "ABEZHIKMNOPTXY"))
    return "".join(f"[{c}{look[c]}]" if c in look else c for c in rx)


HEAD_RX = re.compile(r"(?m)^[ \t]*αρθρο[ \t]+(\d+[ \t]?[α-ωa-z]?|[α-ω]{3,})[ \t]*\.?[ \t]*$")
TOC_RX = re.compile(r"(?m)^[ \t]*αρθρο[ \t]+(\d+[ \t]?[α-ωa-z]?|[α-ω]{3,})[ \t]+(?=\S)")
ACT_HDR_RX = re.compile(_lat(r"(ΝΟΜΟΣ|ΠΡΟΕΔΡΙΚΟ\s+[Δ∆]ΙΑΤΑΓΜΑ|ΝΟΜΟΘΕΤΙΚΟ\s+[Δ∆]ΙΑΤΑΓΜΑ)\s+ΥΠ[’'΄`ʼ]?\s*ΑΡΙΘΜ?\.?\s*(\d+)"
                             r"|(ΠΡΑΞΗ\s+ΝΟΜΟΘΕΤΙΚΟΥ\s+ΠΕΡΙΕΧΟΜΕΝΟΥ)"))
HDR_LINE_RX = re.compile(r"ΕΦΗΜΕΡΙ[ΣΔ∆][AΑ]?\s+[TΤ][HΗ][ΣS]\s+ΚΥΒΕΡΝΗΣΕΩΣ.*|\d{1,6}|[ΤT]εύχος\s+\S+\s+\d+/\S+.*"
                         r"|ΤΕΥΧΟΣ\s+\S+.*|\*\d{8,}\*|Digitally signed.*|Signature Not|Verified|Typografio"
                         r"|Date:.*|Reason:.*|Location:.*", re.I)
_JOIN_RX = re.compile(r"-[ \t]*\n\s*|\s{2,}")


def clean_raw(raw):
    """Αντικαθιστά με κενά (ίδιο μήκος) τις κεφαλίδες σελίδων και τη σήμανση ψηφιακής υπογραφής."""
    spans, off = [], 0
    for page in raw.split("\f"):
        lines, starts, p = page.split("\n"), [], off
        for ln in lines:
            starts.append(p)
            p += len(ln) + 1

        def furn(ln, digits=True):
            s = ln.strip()
            return not s or (HDR_LINE_RX.fullmatch(s) is not None and (digits or not s.isdigit()))

        k = 0
        while k < min(len(lines), 7) and furn(lines[k]):
            spans.append((starts[k], starts[k] + len(lines[k])))
            k += 1
        j = len(lines) - 1
        while j >= k and j >= len(lines) - 10 and furn(lines[j], False):
            spans.append((starts[j], starts[j] + len(lines[j])))
            j -= 1
        off += len(page) + 1
    parts, pos = [], 0
    for a, b in sorted(spans):
        if b > a:
            parts.append(raw[pos:a])
            parts.append(" " * (b - a))
            pos = b
    parts.append(raw[pos:])
    return "".join(parts)


class Doc:
    """Κείμενο ΦΕΚ με κανονικοποιημένη μορφή (norm) για αναζήτηση και αντιστοίχιση θέσεων στο αρχικό."""

    def __init__(self, raw, year=None):
        self.raw, self.year = raw, year
        self.clean = clean_raw(raw)
        self.folded = fold_text(self.clean)
        parts, bn, br, pos, n = [], [0], [0], 0, 0
        for m in _JOIN_RX.finditer(self.folded):
            parts.append(self.folded[pos:m.start()])
            n += m.start() - pos
            if m.group()[0] != "-":
                parts.append(" ")
                n += 1
            pos = m.end()
            bn.append(n)
            br.append(pos)
        parts.append(self.folded[pos:])
        self.norm = "".join(parts)
        for c in "\n\f\t\r\v":
            self.norm = self.norm.replace(c, " ")
        self._bn, self._br = bn, br
        self._pages = [0] + [m.end() for m in re.finditer("\f", raw)]
        self._qs = self._quote_spans()
        self._qstarts = [a for a, _ in self._qs]
        self.headings = []
        for m in HEAD_RX.finditer(self.folded):
            j = m.start() + len(m.group()) - len(m.group().lstrip())
            if not self.clean[j].isupper():
                continue  # «άρθρο 5» σε αλλαγή γραμμής μέσα στο κείμενο, όχι επικεφαλίδα
            p = self.o2n(j)
            self.headings.append({"npos": p, "label": canon_art(m.group(1)),
                                  "num": self.clean[m.start(1):m.end(1)].strip(),
                                  "title": self._title(m.end()), "quoted": self.in_quote(p)})
        self.real = [h for h in self.headings if not h["quoted"]]
        if self.real:
            # τίτλοι που συνεχίζουν σε γραμμή με κεφαλαίο («…Δεδομένων / Προσωπικού Χαρακτήρα»): από τα ΠΕΡΙΕΧΟΜΕΝΑ
            toc = self._toc(self.n2o(self.real[0]["npos"]))
            for h in self.real:
                t = toc.get(h["label"])
                if t and len(t) > len(h["title"]) and _tkey(t).startswith(_tkey(h["title"])):
                    h["title"] = t[:250]
        for i, h in enumerate(self.real):
            h["end"] = self.real[i + 1]["npos"] if i + 1 < len(self.real) else len(self.norm)
        self._hpos = [h["npos"] for h in self.real]
        self.acts = []
        for m in ACT_HDR_RX.finditer(self.clean):
            if m.group(3):
                name = "π.ν.π."
            else:
                g1 = m.group(1).translate(LAT_UP)
                kind = "ν." if g1.startswith("ΝΟΜΟΣ") else "π.δ." if "ΠΡΟΕΔΡ" in g1 else "ν.δ."
                name = f"{kind} {m.group(2)}/{year}" if year else f"{kind} {m.group(2)}"
            self.acts.append((self.o2n(m.start()), name))
        self._apos = [a for a, _ in self.acts]

    def n2o(self, i):
        k = bisect.bisect_right(self._bn, i) - 1
        return min(len(self.raw), self._br[k] + (i - self._bn[k]))

    def o2n(self, j):
        k = bisect.bisect_right(self._br, j) - 1
        v = self._bn[k] + (j - self._br[k])
        return min(v, self._bn[k + 1]) if k + 1 < len(self._bn) else v

    def page_at(self, i):
        return bisect.bisect_right(self._pages, self.n2o(i))

    def upper_at(self, i):
        j = self.n2o(i)
        return j < len(self.clean) and self.clean[j].isupper()

    def cap_word_at(self, i, n):
        """Λέξη με κεφαλαίο αρχικό («Η», «Στο»)· όχι αρίθμηση («Δ’») ή λέξη με κεφαλαία («ΙΙ»)."""
        j = self.n2o(i)
        w = self.clean[j:j + n]
        return w[:1].isupper() and (n == 1 or w[1:2].islower())

    def _quote_spans(self):
        pairs, stack = [], []
        for m in re.finditer(r"[«»“”]", self.norm):
            if m.group() in "«“":
                stack.append(m.start())
            elif stack:
                o = stack.pop()
                n = m.end() - o
                # μεγάλα παραθέματα εισάγονται με «ως εξής:»· αλλιώς πρόκειται για «» που δεν έκλεισε (τυπογραφικό λάθος)
                if n <= 2500 or (n <= 60000 and self.norm[max(0, o - 8):o].rstrip().endswith(":")):
                    pairs.append((o, m.end()))
        spans = []
        for a, b in sorted(pairs):
            if spans and a < spans[-1][1]:
                spans[-1] = (spans[-1][0], max(spans[-1][1], b))
            else:
                spans.append((a, b))
        return spans

    def in_quote(self, i):
        k = bisect.bisect_right(self._qstarts, i) - 1
        return k >= 0 and i < self._qs[k][1]

    def _title(self, j):
        lines = []
        for ln in self.clean[j:j + 900].replace("\f", "\n").split("\n"):
            ln = " ".join(ln.split())  # και τα NBSP («φορείς\xa0-»)
            if ln and not HDR_LINE_RX.fullmatch(ln):
                lines.append(ln)
                if len(lines) == 5:
                    break
        if not lines:
            return ""
        t = lines[0]
        for ln in lines[1:]:
            if t.endswith("-") and not t.endswith(" -") and ln[:1].islower():
                t = t[:-1] + ln
            elif re.match(r"[a-zα-ωάέήίόύώϊϋΐΰ]|\d+/\d|[-–]\s", ln) or t.endswith((" -", "–", ",")):
                t += " " + ln
            else:
                break
        return re.sub(r"\s+", " ", t)[:250]

    def _toc(self, limit):
        """Πίνακας περιεχομένων πριν από το πρώτο άρθρο: «Άρθρο 154 Τίτλος…» σε μία ή περισσότερες γραμμές."""
        toc = {}
        for m in TOC_RX.finditer(self.folded, 0, limit):
            lab = canon_art(m.group(1))
            if lab in toc:
                continue
            lines = self.clean[m.end():min(limit, m.end() + 700)].replace("\f", "\n").split("\n")
            t, n = " ".join(lines[0].split()), 0
            for ln in lines[1:]:
                s = " ".join(ln.split())
                if not s or HDR_LINE_RX.fullmatch(s):
                    continue
                n += 1
                if n > 4 or HEAD_LINE_RX.match(s) or fold(s).startswith("αρθρο ") or _caps(s):
                    break
                t = t[:-1] + s if t.endswith("-") and not t.endswith(" -") and s[:1].islower() else f"{t} {s}"
            toc[lab] = t
        return toc

    def heading_at(self, i):
        k = bisect.bisect_right(self._hpos, i) - 1
        return self.real[k] if k >= 0 else None

    def best_heading(self, label):
        c = [h for h in self.real if h["label"] == label]
        return max(c, key=lambda h: h["end"] - h["npos"]) if c else None

    def act_at(self, i):
        k = bisect.bisect_right(self._apos, i) - 1
        if k >= 0:
            return self.acts[k][1]
        return self.acts[0][1] if self.acts else None

    def snippet(self, a, b, limit=360):
        a, b = max(0, a), min(len(self.norm), b)
        seg = self.clean[self.n2o(a):self.n2o(b)]
        seg = re.sub(r"\s+", " ", re.sub(r"-[ \t]*\n\s*", "", seg)).strip()
        return seg if len(seg) <= limit else seg[:limit].rsplit(" ", 1)[0] + " …"


# ---------------------------------------------------------------- εντοπισμός τροποποιήσεων

_NOUN = (r"(?:τροποποιησ(?:η|ησ|εισ|εων)|αντικαταστασ(?:η|ησ|εισ|εων)|προσθηκ(?:η|ησ|εσ|ων)|καταργησ(?:η|ησ|εισ|εων)"
         r"|συμπληρωσ(?:η|ησ|εισ|εων)|διαγραφ(?:η|ησ|εσ|ων)|αναριθμησ(?:η|ησ|εισ|εων)|καταργουμεν\w*\s+διαταξ\w*)")
_VERB = (r"(?:τροποποι(?:ειται|ουνται)|αντικαθιστ(?:αται|ανται)|προστιθ(?:εται|ενται)|καταργ(?:ειται|ουνται)"
         r"|διαγραφ(?:εται|ονται)|απαλειφ(?:εται|ονται)|συμπληρων(?:εται|ονται)|αναριθμ(?:ειται|ουνται)"
         r"|διορθων(?:εται|ονται)|επερχονται\s+οι\s+(?:ακολουθεσ|εξησ)\s+(?:τροποποιησεισ|αλλαγεσ)"
         r"|παυ(?:ει|ουν)\s+να\s+ισχυ(?:ει|ουν))")
TRIG_RX = re.compile(rf"(?<!\w)(?:(?P<noun>{_NOUN})|(?P<verb>{_VERB}))(?!\w)")
VERB_RX = re.compile(rf"(?<!\w){_VERB}(?!\w)")
_OTHER = r"(?<![\w.])(?:ν|νομου|π\.?\s?δ|ν\.?\s?δ|α\.?\s?ν|β\.?\s?δ|ν\.?\s?π\.?\s?δ)\.?\s*\d{1,5}\s*/\s*\d{2,4}(?!\d)"
OTHER_ACT_RX = re.compile(_OTHER)
_AMENDER = (r"(?:\w*(?:θηκε|θηκαν|θει)|(?:τροποποιησ|αντικαταστασ|προσθηκ|συμπληρωσ|καταργησ)\w*"
            r"(?:\s+(?:του|τησ|των|τουσ))?)\s+(?:με|απο|δυναμει)(?!\w)")
AMENDER_RX = re.compile(_AMENDER)
AMENDER_CLAUSE_RX = re.compile(_AMENDER + rf"[^;:·«»]{{0,160}}?(?:{_OTHER}|(?:του\s+)?παροντοσ)")
BOUNDARY_RX = re.compile(r"[;:·]|\.\s+(?:\d+[α-ω]?\.\s|(?:η|ο|το|οι|τα|με|για|στο|στη|στην|στον|στα|στισ|σε|απο"
                         r"|κατα|μετα|εφοσον|εαν|αν|ωσ|αυτο|επισησ|επιπλεον|ειδικα|ειδικοτερα|ειδικωσ|οσον)\s)")
QUOTE_END_RX = re.compile(r"\s*(?:\([^()]{0,60}\))?\s*[»”]")
AFTER_OK_RX = re.compile(r"\s*(?:[,.;·]|$|(?:περι|οπωσ|και|εκτοσ|πλην|ωσ|που|με)(?!\w))")
LIST_RX = re.compile(r"(?:καταργ(?:ουνται|ειται)|παυ(?:ει|ουν)\s+να\s+ισχυ(?:ει|ουν))(?:\s+(?:οι|η|τα|το))?"
                     r"(?:\s+(?:εξησ|ακολουθεσ|ακολουθα|κατωθι))?(?:\s+διαταξεισ)?\s*:")
CHAIN_WORDS = set("η ο το οι τα τησ του των τη την τον τουσ τισ στο στον στην στη στα στισ στουσ και εωσ μετα πριν "
                  "ωσ προσ ιδιου ιδιο ιδιασ ιδιων ανωτερω αυτου αυτησ αυτο οπωσ ισχυει ισχυουν σημερα εχει εχουν "
                  "που οποιο οποια οποιου οποιασ οποιων οποιοι οποιεσ αρχη αρχησ τελοσ τελουσ εντοσ".split())
CHAIN_STEM_RX = re.compile(
    r"(?:υπο)?(?:παρ|περ|στοιχ|εδ)\w*|(?:αρθ|κεφ|τιτλ|τμημ|ενοτητ|πιν|παραρτημ|διαταξ|φρασ|λεξ)\w*|μερ(?:ουσ|οσ|η)"
    r"|(?:προ)?(?:πρωτ|δευτερ|τριτ|τεταρτ|πεμπτ|εκτ|εβδομ|ογδο|ενατ|δεκατ|ενδεκατ|δωδεκατ|τελευται)"
    r"(?:ο|ου|η|ησ|α|ων|οι|εσ|ουσ)|\w+θηκ(?:ε|αν)|\w+θει|[ivx]+|[ιχ]{2,5}")
TOK_RX = re.compile(r"\x01[^\x02]*\x02|(?<!\w)[α-ωa-z]{1,3}[')]|\d+[α-ωa-z]?(?!\w)\.?|\w+\.?|[^\w\s]")
ITEM_RX = re.compile(r"\s*(?:\d+[α-ω]?\.|[α-ωa-z]{1,3}[')])")
_ART_ITEM = r"(?:\d+(?:[α-ωa-z](?!\w))?|(?:πρωτ|δευτερ|τριτ|τεταρτ|πεμπτ|εκτ|εβδομ|ογδο|ενατ|δεκατ|μον)\w*)"
ART_REF_RX = re.compile(rf"(?<!\w)αρθ(?:ρ(?:ο|ου|α|ων|ουσ))?\.?\s+({_ART_ITEM}(?:\s*(?:,|και|εωσ|-|–)\s*{_ART_ITEM})*)")
PREFIX = {1: r"(?:ν|νομου|νομοσ|νομο)\.?", 2: r"(?:π\.?\s?δ\.?|προεδρικου\s+διαταγματοσ|προεδρικο\s+διαταγμα)",
          3: r"(?:π\.?\s?ν\.?\s?π\.?)", 101: r"(?:α\.?\s?ν\.?|αναγκαστικου\s+νομου|αναγκαστικοσ\s+νομοσ)",
          102: r"(?:β\.?\s?δ\.?|βασιλικου\s+διαταγματοσ|βασιλικο\s+διαταγμα)",
          301: r"(?:ν\.?\s?δ\.?|νομοθετικου\s+διαταγματοσ|νομοθετικο\s+διαταγμα)", 501: r"(?:ν\.?\s?π\.?\s?δ\.?)"}
PERI_RX = re.compile(r"(?<!\w)περι\s[^;:·]*$")  # «…(Α’ 137), περί της λειτουργίας…, καταργείται»
_QM_RX = re.compile(r"«[^«»]*»|“[^“”]*”")
_PM_RX = re.compile(r"\([^()]{0,60}\)")
_PUNCT = (",", "(", ")", "-", "–", "—", "§")


def _blank(m):
    return " " * len(m.group())


def mask(s):
    """Εισαγωγικά → ένα σύμβολο (ίδιο μήκος), σύντομες παρενθέσεις → κενά."""
    for _ in range(2):
        s = _QM_RX.sub(lambda m: "\x01" + " " * (len(m.group()) - 2) + "\x02", s)
    return _PM_RX.sub(_blank, s)


def target_rx(cat, num, year):
    yy = f"(?:{year}|{year % 100:02d})"
    return re.compile(rf"(?:(?<![\w.]){PREFIX.get(cat, PREFIX[1])}\s*|(?<=και )|(?<=, ))0*{num}\s*/\s*{yy}(?!\d)")


def tok_ok(t):
    """Επιτρεπτό στοιχείο σε αλυσίδα διατάξεων («η περ. α' της παρ. 2 του άρθρου 5 του»)."""
    c = t[0]
    if c in "\x01\x03" or t in _PUNCT or c.isdigit() or t[-1] in "')":
        return True
    if t.endswith("."):  # συντομογραφία (παρ., περ.)· τελεία μετά από πλήρη λέξη σημαίνει τέλος πρότασης
        w = t[:-1]
        return len(w) == 1 or bool(CHAIN_STEM_RX.fullmatch(w))
    return t in CHAIN_WORDS or (len(t) == 1 and t.isalpha()) or bool(CHAIN_STEM_RX.fullmatch(t))


def chain_ok(s):
    return all(tok_ok(m.group()) for m in TOK_RX.finditer(s))


def articles_in(s):
    out = set()
    for m in ART_REF_RX.finditer(s):
        parts = re.split(r"\s*(,|και|εωσ|-|–)\s*", m.group(1))
        items, seps = parts[0::2], parts[1::2]
        for i, it in enumerate(items):
            out.add(canon_art(it))
            if i and seps[i - 1] in ("εωσ", "-", "–") and items[i - 1].isdigit() and it.isdigit():
                a, b = int(items[i - 1]), int(it)
                if 0 < b - a <= 60:
                    out.update(str(x) for x in range(a + 1, b))
    return out


def kind_of(t):
    for stems, k in ((("καταργ", "παυ"), "κατάργηση"), (("αντικα",), "αντικατάσταση"),
                     (("προστι", "προσθηκ"), "προσθήκη"), (("διαγραφ", "απαλειφ"), "διαγραφή"),
                     (("συμπληρ",), "συμπλήρωση"), (("αναριθμ",), "αναρίθμηση"), (("διορθ",), "διόρθωση σφάλματος")):
        if t.startswith(stems):
            return k
    return "τροποποίηση"


def mask_acts(s):
    return OTHER_ACT_RX.sub(lambda m: "\x03" * len(m.group()), s)


def after_last_act(chain):
    k = chain.rfind("\x03")
    return chain[k + 1:] if k >= 0 else chain


def subject(doc, s):
    """Η αλυσίδα διατάξεων («Η παρ. 2 του άρθρου 5 του») αμέσως πριν από την παραπομπή.
    → (θέση έναρξης, αλυσίδα, τμήμα μετά την τελευταία άλλη πράξη)."""
    lo = max(0, s - 260)
    w = mask_acts(mask(doc.norm[lo:s]))
    toks = list(TOK_RX.finditer(w))
    i = len(toks)
    while i > 0 and tok_ok(toks[i - 1].group()):
        i -= 1
        if doc.cap_word_at(lo + toks[i].start(), len(toks[i].group())):
            break  # κεφαλαίο αρχικό: αρχή πρότασης/στοιχείου
    while i < len(toks) and toks[i].group() in _PUNCT + ("\x03" * len(toks[i].group()),):
        i += 1
    st = toks[i].start() if i < len(toks) else len(w)
    chain = w[st:]
    return lo + st, chain, after_last_act(chain)


def starts_clause(doc, cs, chain):
    if ITEM_RX.match(chain):
        return True
    if cs < len(doc.norm) and doc.upper_at(cs):
        return True
    prev = doc.norm[max(0, cs - 4):cs].rstrip()
    return not prev or prev[-1] in ".;:·,"


def _before(doc, s):
    """«Τροποποίηση/Καταργείται η παρ. 2 του άρθρου 5 του ν. Χ»."""
    lo = max(0, s - 220)
    w = mask(doc.norm[lo:s])
    t = None
    for t in TRIG_RX.finditer(w):
        pass
    if t is None:
        return None
    gap = mask_acts(w[t.end():])  # «Τροποποίηση άρθρου 66 ν. 2121/1993 και άρθρου 18 ν. 4624/2019»
    if len(gap) > 160 or BOUNDARY_RX.search(gap) or not chain_ok(gap):
        return None
    ts = lo + t.start()
    if "[" in doc.norm[max(0, ts - 2):ts] or (t.group("noun") and not doc.upper_at(ts)):
        return None
    return {"pos": s, "form": "before", "kind": kind_of(t.group()), "targets": articles_in(after_last_act(gap)),
            "a": ts, "b": s + 200}


def _after(doc, s, e):
    """«Η παρ. 2 του άρθρου 5 του ν. Χ (Α' 1), όπως ισχύει, αντικαθίσταται ως εξής:»."""
    w = mask(doc.norm[e:e + 1200])
    v = VERB_RX.search(w, 0, 400)
    if not v or v.start() > 320:
        return None
    gap = AMENDER_CLAUSE_RX.sub(_blank, w[:v.start()])
    if BOUNDARY_RX.search(gap):
        return None
    if OTHER_ACT_RX.search(gap):
        if not re.search(r"(?:νται|ουν)$", v.group()):
            return None
        gap = OTHER_ACT_RX.sub(_blank, gap)
    gap = PERI_RX.sub(_blank, gap)
    if not chain_ok(gap):
        return None
    cs, chain, tchain = subject(doc, s)
    if not starts_clause(doc, cs, chain):
        return None
    kind = kind_of(v.group())
    tg = articles_in(tchain) | articles_in(gap)
    if kind in ("προσθήκη", "αναρίθμηση"):
        tg |= articles_in(re.split(r"[:;·\x01«]|ωσ\s+εξησ|ωσ\s+ακολουθωσ", w[v.end():v.end() + 120])[0])
    return {"pos": s, "form": "after", "kind": kind, "targets": tg, "a": cs, "b": e + v.end() + 120}


def _listed(doc, s, e):
    """Στοιχείο λίστας σε άρθρο «Καταργούμενες διατάξεις» ή μετά από «καταργούνται:»."""
    h = doc.heading_at(s)
    lo = h["npos"] if h else max(0, s - 6000)
    if not (h and "καταργ" in fold(h["title"])) and not LIST_RX.search(doc.norm, lo, s):
        return None
    cs, chain, tchain = subject(doc, s)
    if not (ITEM_RX.match(chain) or doc.norm[max(0, cs - 4):cs].rstrip()[-1:] in (",", ";", ":", "·")):
        return None
    if not AFTER_OK_RX.match(mask(doc.norm[e:e + 200])):
        return None
    return {"pos": s, "form": "list", "kind": "κατάργηση", "targets": articles_in(tchain), "a": cs, "b": e + 160}


def scan(doc, rx):
    norm, hits = doc.norm, []
    for m in rx.finditer(norm):
        s, e = m.span()
        if doc.in_quote(s) or QUOTE_END_RX.match(norm, e):
            continue  # η παραπομπή βρίσκεται μέσα σε παρατιθέμενο κείμενο
        pre = norm[max(0, s - 90):s]
        am = None
        for am in AMENDER_RX.finditer(pre):
            pass
        if am is not None and not OTHER_ACT_RX.search(pre, am.end()):
            continue  # «όπως τροποποιήθηκε με ... ν. Χ»: εδώ ο ν. Χ είναι ο τροποποιών
        # και οι δύο μορφές: «Τροποποίηση άρθρου 47 ν. Χ — Το δεύτερο εδάφιο του άρθρου 47 του ν. Χ καταργείται»
        hs = [h for h in (_before(doc, s), _after(doc, s, e)) if h] or [h for h in (_listed(doc, s, e),) if h]
        hits.extend(hs)
    return hits


def group_hits(doc, f, hits):
    groups = {}
    for h in hits:
        hd = doc.heading_at(h["pos"])
        k = (doc.act_at(h["pos"]), hd["label"] if hd else None)
        if k not in groups:
            best = doc.best_heading(hd["label"]) if hd else None
            groups[k] = {"fek": f, "act": k[0], "article": best["num"] if best else None,
                         "title": best["title"] if best else "", "span": (best["npos"], best["end"]) if best else None,
                         "kinds": [], "targets": set(), "forms": set(), "hits": [],
                         "mentions": articles_in(doc.norm[best["npos"]:best["end"]]) if best else set()}
        g = groups[k]
        if h["kind"] not in g["kinds"]:
            g["kinds"].append(h["kind"])
        g["targets"] |= h["targets"]
        g["forms"].add(h["form"])
        g["hits"].append(h)
    out = []
    for g in groups.values():
        sp = g.pop("span")
        hs = sorted(g.pop("hits"), key=lambda h: (not (sp and sp[0] <= h["pos"] < sp[1]), h["pos"]))
        g["page"] = doc.page_at(hs[0]["pos"])
        g["snippets"], last = [], -10 ** 9
        for h in hs:
            if len(g["snippets"]) < 2 and abs(h["a"] - last) > 150:
                g["snippets"].append(doc.snippet(h["a"], h["b"]))
                last = h["a"]
        g["forms"] = sorted(g["forms"])
        out.append(g)
    named = [g for g in out if g["article"]]
    covered = set().union(*(g["targets"] for g in named)) if named else set()
    # ευρήματα πριν από το πρώτο άρθρο (π.χ. πίνακας περιεχομένων) κρατούνται μόνο αν προσθέτουν κάτι
    return [g for g in out if g["article"] or not named or (g["targets"] and not g["targets"] <= covered)]


# ---------------------------------------------------------------- εμφάνιση

def emit_json(obj):
    def conv(o):
        if isinstance(o, set):
            return sorted(o, key=art_sort_key)
        raise TypeError(type(o))
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=conv))


def fek_line(f):
    s = f"ΦΕΚ {f['label']}"
    if f.get("pages"):
        s += f" ({f['pages']} σελ.)"
    return s


PARA_RX = re.compile(r"(?:\d+[α-ωΑ-Ωa-zA-Z]?\.\s|\(?[α-ωa-zΑ-Ω]{1,3}[)΄’']\s|[«“]|[-–•]\s)")
HEAD_LINE_RX = re.compile(r"(?:Άρθρο|ΑΡΘΡΟ|Αρθρο)\s+\S+\s*\.?$")


def _caps(s):
    letters = [c for c in s if c.isalpha()]
    return len(letters) >= 3 and all(c.isupper() for c in letters)


def pretty(seg):
    """Ενώνει τις γραμμές του PDF σε παραγράφους."""
    out = []
    for ln in seg.replace("\f", "\n").split("\n"):
        s = ln.strip()
        if not s:
            continue
        if out:
            prev = out[-1]
            if prev.endswith("-") and not prev.endswith(" -") and s[:1].islower() and not PARA_RX.match(s):
                out[-1] = prev[:-1] + s
                continue
            title_ctx = len(out) >= 2 and HEAD_LINE_RX.match(out[-2]) and not s[:1].islower()
            if not (PARA_RX.match(s) or HEAD_LINE_RX.match(s) or HEAD_LINE_RX.match(prev) or prev.endswith(":")
                    or _caps(s) or _caps(prev) or title_ctx):
                out[-1] = prev + " " + s
                continue
        out.append(s)
    return "\n".join(out)


def clip(text, n):
    if not n or len(text) <= n:
        return text, False
    return text[:n].rsplit(" ", 1)[0] + " …", True


# ---------------------------------------------------------------- υποεντολές

def cmd_find(a):
    kind, t, num, year = parse_ref(a.ref)
    note = None
    if kind == "fek":
        f = resolve_fek(t, num, year)
        if not f:
            die(f"Δεν βρέθηκε ΦΕΚ {ISSUES.get(t, t)} {num}/{year}.", 1)
        res = [{"act": None, "fek": f}]
    else:
        hits, note = lookup_act(t, parse_type(a.type), num, year)
        if not hits:
            die(f"Δεν βρέθηκε πράξη {num}/{year} στο search.et.gr (έλεγχος {date.today():%d.%m.%Y}).", 1)
        res = [{"act": f"{ACT_TYPES[c]} {num}/{year}", "fek": f} for c, f in hits]
    if a.json:
        return emit_json({"note": note, "results": res})
    if note:
        print(note)
    for r in res:
        f = r["fek"]
        print(f"{r['act'] + ' — ' if r['act'] else ''}{fek_line(f)}")
        if f["title"]:
            print(f"  Τίτλος: {f['title'][:400]}")
        if f["url"]:
            print(f"  Σελίδα ΦΕΚ: {f['url']}")
        print(f"  PDF: {f['pdf']}")


def cmd_text(a):
    act, f = resolve(a.ref, parse_type(a.type))
    raw = fek_text(f)
    head = f"{act['name'] + ' — ' if act else ''}{fek_line(f)}\nPDF: {f['pdf']}"
    warn = "ΠΡΟΣΟΧΗ: κείμενο όπως ΔΗΜΟΣΙΕΥΤΗΚΕ, όχι ενοποιημένο. Για μεταγενέστερες αλλαγές: amendments."
    if len(raw.strip()) < 200:
        warn += " Το PDF δεν έχει επίπεδο κειμένου (πιθανώς σαρωμένο)· άνοιξέ το απευθείας."
    res = {"act": act, "fek": f, "warning": warn}
    if a.grep:
        doc = Doc(raw, f["year"])
        toks = re.findall(r"\w+|[^\w\s]", fold(a.grep))
        if not toks:
            die("Κενό --grep.")
        rx = re.compile(r"\s*".join(re.escape(t) for t in toks))
        spans = []  # [αρχή, τέλος, εμφανίσεις, θέση 1ης]· κοντινές εμφανίσεις σε ένα απόσπασμα
        for m in rx.finditer(doc.norm):
            lo, hi = m.start() - 220, m.end() + 220
            if spans and lo <= spans[-1][1] and hi - spans[-1][0] <= 900:
                spans[-1][1] = hi
                spans[-1][2] += 1
            else:
                spans.append([lo, hi, 1, m.start()])
        res["total"] = sum(s[2] for s in spans)
        res["matches"], used = [], 0
        for lo, hi, k, st in spans[:max(a.limit, 1)]:
            t = doc.snippet(lo, hi, 900)
            if res["matches"] and a.max_chars and used + len(t) > a.max_chars:
                break
            used += len(t)
            res["matches"].append({"page": doc.page_at(st), "hits": k, "text": t})
        shown = sum(m["hits"] for m in res["matches"])
        if a.json:
            return emit_json(res)
        print(head)
        more = f" (εμφανίζονται {shown}· περιόρισε τη φράση ή άλλαξε --limit/--max-chars)" if shown < res["total"] else ""
        print(f"Αναζήτηση «{a.grep}»: {res['total']} εμφανίσεις{more}")
        for m in res["matches"]:
            print(f"\n[σελ. {m['page']}] …{m['text']}…")
        print(f"\n{warn}")
        return
    if a.article:
        doc = Doc(raw, f["year"])
        lab = canon_art(a.article)
        cands = [h for h in doc.real if h["label"] == lab] or [h for h in doc.headings if h["label"] == lab]
        if not cands:
            die(f"Δεν βρέθηκε επικεφαλίδα «Άρθρο {a.article}» στο ΦΕΚ {f['label']}. Δοκίμασε --grep.", 1)
        h = max(cands, key=lambda h: h.get("end", h["npos"]) - h["npos"])
        end = h.get("end") or min(len(doc.norm), h["npos"] + 20000)
        lines = pretty(doc.clean[doc.n2o(h["npos"]):doc.n2o(end)]).split("\n")
        while len(lines) > 2 and _caps(lines[-1]):
            lines.pop()  # επικεφαλίδες της επόμενης ενότητας («ΜΕΡΟΣ ΙΑ’», «ΚΕΦΑΛΑΙΟ Β’»)
        text = "\n".join(lines)
        res.update({"article": h["num"], "title": h["title"],
                    "pages": [doc.page_at(h["npos"]), doc.page_at(max(h["npos"], end - 1))]})
    else:
        text = pretty(clean_raw(raw))
    text, cut = clip(text, a.max_chars)
    res.update({"text": text, "truncated": cut})
    if a.json:
        return emit_json(res)
    print(head)
    if a.article:
        print(f"Άρθρο {res['article']} — σελ. {res['pages'][0]}–{res['pages'][1]}")
    print(f"{warn}\n\n{text}")
    if cut:
        print(f"\n[Περικόπηκε στους {a.max_chars} χαρακτήρες· χρησιμοποίησε --article, --grep ή --max-chars 0]")


def cmd_amendments(a):
    t0 = time.time()
    act, own = resolve(a.ref, parse_type(a.type))
    if act is None:
        die("Το amendments δέχεται πράξη (π.χ. 4624/2019 ή «π.δ. 80/2016»), όχι ετικέτα ΦΕΚ.")
    cat, num, year = act["type"], act["number"], act["year"]
    since = parse_day(a.since) if a.since else date.fromisoformat(own["date"]) if own["date"] else date(year, 1, 1)
    until = parse_day(a.until, end=True) if a.until else date.today()
    issues = parse_issues(a.issue)
    rows, capped = search_range(f"{num}/{year}", issues, since, until)
    feks = sorted((x for x in (fek_info(r) for r in rows) if x["id"] != own["id"]), key=lambda x: x["date"] or "")
    pages = sum(x["pages"] for x in feks)
    iss = "όλα τα τεύχη" if not issues else "τεύχος " + ", ".join(ISSUES.get(int(i), i) for i in issues)
    eprint(f"{act['name']} ({own['label']}): {len(feks)} ΦΕΚ ({iss}, {since:%d.%m.%Y}–{until:%d.%m.%Y}) "
           f"αναφέρουν «{num}/{year}» — {pages} σελίδες, αναζήτηση {time.time() - t0:.1f}s")
    if len(feks) > a.max_docs:
        die(f"Πάρα πολλά ΦΕΚ ({len(feks)} > --max-docs {a.max_docs}). Περιόρισε με --since/--until "
            f"(π.χ. --since {max(since.year, until.year - 3)}) ή αύξησε το --max-docs.")
    if any(not os.path.exists(cache_path(x)) for x in feks):
        pdf_backend()  # έλεγχος μία φορά εδώ, όχι σε κάθε διεργασία
    groups, errors, scanned, cached, n = [], [], [], 0, len(feks)
    for i, r in enumerate(analyze_all(feks, cat, num, year, a.workers), 1):
        f = r["fek"]
        cached += r["cached"]
        if r["error"]:
            errors.append(f"{f['label']}: {r['error']}")
        if r["scanned"]:
            scanned.append(f["label"])
        groups.extend(r["groups"])
        if i % 25 == 0 or i == n:
            eprint(f"  {i}/{n} ΦΕΚ (από cache: {cached})")
    groups.sort(key=lambda g: (g["fek"]["date"] or "", g["fek"]["issue"], g["fek"]["number"], g["page"]))
    errors.sort()
    scanned.sort()
    hidden = 0
    if a.article:
        lab = canon_art(a.article)
        keep = [g for g in groups if lab in g["targets"]
                or (not g["targets"] and ("κατάργηση" in g["kinds"] or lab in g["mentions"]))]
        hidden, groups = len(groups) - len(keep), keep
    for g in groups:
        g.pop("mentions", None)
    secs = time.time() - t0
    res = {"act": act, "fek": own, "since": since.isoformat(), "until": until.isoformat(), "issues": iss,
           "searched": len(feks), "pages": pages, "capped": capped, "article": a.article, "results": groups,
           "hidden": hidden, "errors": errors, "scanned": scanned, "seconds": round(secs, 1),
           "checked": date.today().isoformat()}
    if a.json:
        return emit_json(res)
    print(f"{act['name']} — {fek_line(own)}")
    print(f"Ελέγχθηκαν {len(feks)} ΦΕΚ ({iss}) από {since:%d.%m.%Y} έως {until:%d.%m.%Y} σε {secs:.0f}s "
          f"(από cache: {cached}).")
    if capped:
        print("ΠΡΟΣΟΧΗ: το API έφτασε το όριο αποτελεσμάτων· μπορεί να λείπουν ΦΕΚ.")
    flt = f" για το άρθρο {a.article} ({hidden} άλλα κρύφτηκαν)" if a.article else ""
    print(f"Πιθανές τροποποιήσεις/καταργήσεις{flt}: {len(groups)}\n")
    for i, g in enumerate(groups, 1):
        f = g["fek"]
        line = f"[{i}] {g['act'] or 'πράξη'} · ΦΕΚ {f['label']}"
        if g["article"]:
            line += f" · άρθρο {g['article']}" + (f" «{clip(g['title'], 200)[0]}»" if g["title"] else "")
        print(f"{line} · σελ. {g['page']}")
        tg = ", ".join(sorted(g["targets"], key=art_sort_key))
        print(f"    {', '.join(g['kinds'])} → " + (f"άρθρα {tg}" if tg else "(δεν εντοπίστηκε αριθμός άρθρου)"))
        for s in g["snippets"]:
            print(f"    «{s}»")
        print(f"    {f['pdf']}#page={g['page']}")
    if errors:
        print(f"\nΣφάλματα λήψης ({len(errors)}): " + "; ".join(errors[:5]))
    if scanned:
        print(f"\nΣαρωμένα PDF χωρίς κείμενο (έλεγξέ τα χειροκίνητα): {', '.join(scanned)}")
    print("\nΣημείωση: αυτόματος εντοπισμός. Επιβεβαίωσε κάθε εύρημα στο ΦΕΚ, π.χ.: "
          "python tools/fek.py text \"Α 91/2023\" --article 123\n"
          "Δεν εντοπίζονται: σιωπηρές καταργήσεις, παραπομπές χωρίς αριθμό νόμου («του ίδιου νόμου»), "
          "ΦΕΚ εκτός των τευχών που ελέγχθηκαν.")


def cmd_search(a):
    issues = parse_issues(a.issue, default=())
    d1 = parse_day(a.date_from) if a.date_from else None
    d2 = parse_day(a.date_to, end=True) if a.date_to else None
    if d1 or d2:
        d1, d2 = d1 or date(1900, 1, 1), d2 or date.today()
    rows = simple_search(a.text, issues, d1, d2, years=[a.year] if a.year else None)
    res = []
    for r in rows:
        f = fek_info(r)
        snip = ""
        try:
            snip = re.sub(r"\s+", " ", base64.b64decode(r.get("search_MatchedText") or "").decode("utf-8", "replace"))
        except ValueError:
            pass
        f["match"] = re.sub(r"<[^>]+>", "", snip).strip()[:300]
        res.append(f)
    res.sort(key=lambda x: x["date"] or "", reverse=True)
    total = len(res)
    res = res[:a.limit]
    if a.json:
        return emit_json({"query": a.text, "total": total, "capped": total >= MAX_RESULTS, "results": res})
    print(f"«{a.text}»: {total}{'+ (όριο API)' if total >= MAX_RESULTS else ''} ΦΕΚ· εμφανίζονται {len(res)} "
          f"(νεότερα πρώτα)")
    for f in res:
        print(f"- {fek_line(f)}" + (f" — {f['title'][:160]}" if f["title"] else ""))
        if f["match"]:
            print(f"    …{f['match']}…")
        print(f"    {f['pdf']}")
    print("\nΣυμβουλή: λειτουργούν καλύτερα μεμονωμένοι όροι ή «N/ΕΕΕΕ»· οι πολυλεκτικές αναζητήσεις είναι ασταθείς.")


def th_get(path):
    try:
        return http_get(TAXHEAVEN + path, headers={"User-Agent": UA, "Accept-Language": "el"}, timeout=30) \
            .decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def html_text(s):
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr)>", "\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    return "\n".join(re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in s.split("\n") if ln.strip())


def cmd_crosscheck(a):
    kind, t, num, year = parse_ref(a.ref)
    if kind == "fek" or (t or 1) != 1:
        die("Το crosscheck υποστηρίζει μόνο νόμους (π.χ. 4624/2019).")
    page = th_get(f"/{num}/{year}")
    res = {"source": "taxheaven.gr", "law": f"ν. {num}/{year}", "url": f"{TAXHEAVEN}/{num}/{year}",
           "checked": date.today().isoformat(), "found": page is not None}
    if page is not None:
        m = re.search(r"(?is)<title>(.*?)</title>", page)
        res["title"] = re.sub(r"\s+", " ", html.unescape(m.group(1))).strip() if m else ""
        c = re.search(r"Κωδικοποιημένος με\s*([\d/ ,]+)", res["title"])
        res["codified_with"] = c.group(1).strip(" ,") if c else None
    if page is not None and a.article:
        art = a.article.strip().upper().translate(str.maketrans("ABEZHIKMNOPTYX", "ΑΒΕΖΗΙΚΜΝΟΡΤΥΧ"))
        path = f"/{num}/{year}/arthro/{urllib.parse.quote(art)}"
        ap = th_get(path)
        res.update({"article": art, "article_url": TAXHEAVEN + path, "article_found": ap is not None})
        if ap is not None:
            i, j = ap.find("lawt-artview-body"), ap.find("lawt-artview-actions")
            body = ap[ap.rfind("<", 0, i):ap.rfind("<", 0, j) if j > i else len(ap)] if i >= 0 else ap
            body = re.sub(r"(?s)<[^>]*lawt-artview-para-label[^>]*>(.*?)</[^>]+>", r"\n\1 ", body)
            text = re.sub(r"(?m)^§\s*(\S+)\n(?=\1\.?\s)", "", html_text(body))  # «§ 1» πριν από «1. …»
            res["repealed_paragraphs"] = [x or y for x, y in re.findall(
                r"(?m)^(?:§\s*(\S+)\s*)?(?:(\d+[α-ω]?)\.?)?\s*\.{4,}\s*$", text) if x or y]
            res["text"], res["truncated"] = clip(text, a.max_chars)
    if a.json:
        return emit_json(res)
    print(f"taxheaven.gr — δευτερογενής πηγή, ΜΟΝΟ για διασταύρωση (έλεγχος {date.today():%d.%m.%Y})")
    if page is None:
        print(f"Δεν βρέθηκε ο ν. {num}/{year} στο taxheaven.gr ({res['url']}).")
        return
    print(f"{res['title']}\n{res['url']}")
    print("Κωδικοποίηση: " + (f"έως τον ν. {res['codified_with']}" if res["codified_with"]
                              else "δεν αναφέρεται (πιθανώς μόνο το αρχικό κείμενο)"))
    if a.article:
        if not res["article_found"]:
            print(f"\nΔεν βρέθηκε άρθρο {res['article']} ({res['article_url']}).")
            return
        print(f"\n{res['article_url']}")
        if res["repealed_paragraphs"]:
            print("Παράγραφοι με τελείες (καταργημένες): " + ", ".join(res["repealed_paragraphs"]))
        print(f"\n{res['text']}")
        if res["truncated"]:
            print(f"\n[Περικόπηκε στους {a.max_chars} χαρακτήρες· χρησιμοποίησε --max-chars 0]")
    print("\nΠΡΟΣΟΧΗ: χωρίς σημειώσεις τροποποιήσεων· η τελική επιβεβαίωση γίνεται στο ΦΕΚ.")


def main(argv=None):
    for st in (sys.stdout, sys.stderr):
        if hasattr(st, "reconfigure"):
            st.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="fek.py", description="Έρευνα ΦΕΚ μέσω του API του search.et.gr.",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sp = p.add_subparsers(dest="cmd", required=True)

    def sub(name, fn, help_):
        s = sp.add_parser(name, help=help_)
        s.add_argument("--json", action="store_true", help="έξοδος σε JSON")
        s.set_defaults(fn=fn)
        return s

    s = sub("find", cmd_find, "εντοπισμός ΦΕΚ πράξης")
    s.add_argument("ref", help="π.χ. 4624/2019, «π.δ. 80/2016» ή «Α 91/2023»")
    s.add_argument("--type", help="νόμος (προεπιλογή), πδ, πνπ, αν, βδ, νδ, νπδ ή all")

    s = sub("text", cmd_text, "κείμενο ΦΕΚ όπως δημοσιεύτηκε")
    s.add_argument("ref", help="πράξη (4624/2019) ή ετικέτα ΦΕΚ («Α 91/2023»)")
    s.add_argument("--type", help="είδος πράξης, όπως στο find")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--article", help="μόνο το άρθρο (π.χ. 11, 84Α, πρώτο)")
    g.add_argument("--grep", help="αναζήτηση φράσης (χωρίς τόνους/πεζά-κεφαλαία)")
    s.add_argument("--limit", type=int, default=15, help="μέγιστα αποσπάσματα για --grep")
    s.add_argument("--max-chars", type=int, default=9000,
                   help="όριο χαρακτήρων (0 = χωρίς όριο)· η προεπιλογή χωράει στην έξοδο εργαλείου των agents")

    s = sub("amendments", cmd_amendments, "μεταγενέστερα ΦΕΚ που τροποποιούν/καταργούν")
    s.add_argument("ref", help="πράξη, π.χ. 4624/2019 ή «π.δ. 80/2016»")
    s.add_argument("--type", help="είδος πράξης, όπως στο find")
    s.add_argument("--article", help="μόνο ευρήματα για το άρθρο")
    s.add_argument("--since", help="από ΕΕΕΕ-ΜΜ-ΗΗ ή ΕΕΕΕ (προεπιλογή: ημερομηνία ΦΕΚ της πράξης)")
    s.add_argument("--until", help="έως ΕΕΕΕ-ΜΜ-ΗΗ ή ΕΕΕΕ (προεπιλογή: σήμερα)")
    s.add_argument("--issue", help="τεύχη, π.χ. Α (προεπιλογή), «Α,Δ» ή all")
    s.add_argument("--max-docs", type=int, default=400, help="μέγιστο πλήθος ΦΕΚ προς έλεγχο")
    s.add_argument("--workers", type=int, default=min(12, os.cpu_count() or 4),
                   help="παράλληλες διεργασίες για λήψη/ανάλυση (1 = σειριακά)")

    s = sub("search", cmd_search, "αναζήτηση πλήρους κειμένου")
    s.add_argument("text", help="φράση προς αναζήτηση στο κείμενο των ΦΕΚ")
    s.add_argument("--issue", help="τεύχη, π.χ. Β ή «Α,Β» (προεπιλογή: όλα)")
    s.add_argument("--year", type=int)
    s.add_argument("--from", dest="date_from", help="ΕΕΕΕ-ΜΜ-ΗΗ")
    s.add_argument("--to", dest="date_to", help="ΕΕΕΕ-ΜΜ-ΗΗ")
    s.add_argument("--limit", type=int, default=30)

    s = sub("crosscheck", cmd_crosscheck, "διασταύρωση με κωδικοποιημένο κείμενο (taxheaven.gr)")
    s.add_argument("ref", help="νόμος, π.χ. 4624/2019")
    s.add_argument("--article", help="άρθρο, π.χ. 11 ή 84Α")
    s.add_argument("--max-chars", type=int, default=9000, help="όριο χαρακτήρων (0 = χωρίς όριο)")

    a = p.parse_args(argv)
    try:
        a.fn(a)
    except urllib.error.HTTPError as e:
        die(f"Σφάλμα HTTP {e.code}: {e.url}")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        die(f"Σφάλμα δικτύου: {e}")


if __name__ == "__main__":
    main()
