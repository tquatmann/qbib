#!/usr/bin/env python3
import argparse
import gzip
import html.entities
import json
import os
import re
import sqlite3
import sys
import time
import shutil
import subprocess
import unicodedata
import webbrowser
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from html import unescape

BIB_REPLACEMENTS = [
    ("Markov", r"{M}arkov"),
    ("MDP", r"{MDP}"),
    (r"PO{MDP}", r"{POMDP}"),
    (r"I{MDP}", r"{IMDP}"),
    ("Storm", r"{S}torm"),
    ("iscasMc", r"{iscasMc}"),
    ("QComp", r"{QComp}"),
    ("PRISM", r"{PRISM}"),
    ("MultiGain", r"{MultiGain}"),
    ("The Modest Toolset", r"{T}he {M}odest {T}oolset"),
    (r"Kret{\'{\i}}nsk{\'{y}}", r"K{\v r}et{\' i}nsk{\' y}"),
]

# DBLP data dump mirrors (the search API is behind a bot check, the dump is not)
DUMP_URLS = [
    "https://dblp.org/xml/dblp.xml.gz",
    "https://dblp.uni-trier.de/xml/dblp.xml.gz",
    "https://dblp.dagstuhl.de/xml/dblp.xml.gz",
]

# record types of dblp.xml that we index (everything else, e.g. <www> homepages, is skipped)
RECORD_TAGS = {"article", "inproceedings", "proceedings", "book", "incollection",
               "phdthesis", "mastersthesis", "data"}
# child elements that we keep for every record
FIELD_TAGS = {"author", "editor", "title", "booktitle", "journal", "volume", "number",
              "pages", "year", "publisher", "series", "school", "isbn", "crossref", "ee",
              "chapter"}
# order in which fields are written to BibTeX
BIB_FIELD_ORDER = ["author", "editor", "title", "booktitle", "journal", "volume", "number",
                   "chapter", "pages", "year", "publisher", "series", "school", "isbn"]


def data_dir() -> str:
    base = os.environ.get("QBIB_DATA_DIR")
    if not base:
        base = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "qbib")
    return base


def db_path() -> str:
    return os.path.join(data_dir(), "dblp.sqlite")


# ---------------------------------------------------------------------------
# Updating the local index
# ---------------------------------------------------------------------------

class CountingReader:
    """Wraps a response and reports download progress on stderr."""
    def __init__(self, resp, total):
        self.resp, self.total, self.n, self.last = resp, total, 0, 0.0

    def read(self, size=-1):
        chunk = self.resp.read(size)
        self.n += len(chunk)
        now = time.time()
        if now - self.last > 0.5 or not chunk:
            self.last = now
            pct = f" ({100 * self.n / self.total:.0f}%)" if self.total else ""
            print(f"\r  read {self.n / 1e6:,.0f} MB{pct}", end="", file=sys.stderr, flush=True)
        return chunk


def open_dump(url: str, timeout: float = 30.0):
    req = urllib.request.Request(url, headers={"User-Agent": "qbib/2.0 (local dblp index)"})
    return urllib.request.urlopen(req, timeout=timeout)


def head_dump(url: str, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "qbib/2.0 (local dblp index)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return {k.lower(): v for k, v in resp.headers.items()}


def record_from_elem(elem):
    fields = {}
    for child in elem:
        if child.tag in FIELD_TAGS:
            # itertext flattens inline markup like <i>, <sub>, <sup>
            text = "".join(child.itertext()).strip()
            if text:
                fields.setdefault(child.tag, []).append(text)
    return {"type": elem.tag, "key": elem.get("key"), "f": fields}


def index_stream(stream, conn):
    """Parse a dblp.xml stream and fill the database. Returns the number of records."""
    parser = ET.XMLParser()
    # dblp.xml uses entities (&uuml; ...) declared in dblp.dtd, which expat does not load
    for name, cp in html.entities.name2codepoint.items():
        parser.entity[name] = chr(cp)
    root = None
    count = 0
    batch = []

    def flush():
        cur = conn.cursor()
        for rec, (title, authors, venue, year) in batch:
            cur.execute("INSERT INTO rec(key, data) VALUES (?, ?)", (rec["key"], json.dumps(rec, ensure_ascii=False)))
            cur.execute("INSERT INTO fts(rowid, title, authors, venue, year) VALUES (?, ?, ?, ?, ?)",
                        (cur.lastrowid, title, authors, venue, year))
        batch.clear()

    for event, elem in ET.iterparse(stream, events=("start", "end"), parser=parser):
        if event == "start":
            if root is None:
                root = elem
            continue
        if elem.tag not in RECORD_TAGS:
            continue
        rec = record_from_elem(elem)
        elem.clear()
        root.clear()
        if not rec["key"] or "title" not in rec["f"]:
            continue
        info = info_from_record(rec)
        batch.append((rec, (info["title"], info["authors"], info["venue"], info["year"])))
        count += 1
        if len(batch) >= 20000:
            flush()
    flush()
    return count


def update_index() -> int:
    os.makedirs(data_dir(), exist_ok=True)
    final = db_path()
    tmp = final + ".new"

    # find a mirror that answers and check whether we are already up to date
    url, meta = None, None
    for candidate in DUMP_URLS:
        try:
            meta = head_dump(candidate)
            url = candidate
            break
        except Exception as e:
            print(f"Could not reach {candidate}: {e}", file=sys.stderr)
    if url is None:
        print("No DBLP mirror reachable.", file=sys.stderr)
        return 1
    remote_id = f"{url}|{meta.get('etag', '')}|{meta.get('last-modified', '')}"
    if os.path.exists(final):
        try:
            with sqlite3.connect(final) as conn:
                local = dict(conn.execute("SELECT k, v FROM meta").fetchall())
            if local.get("source") == remote_id:
                print(f"Local index is already up to date ({local.get('records')} records, "
                      f"DBLP dump from {meta.get('last-modified', 'unknown date')}).")
                return 0
        except sqlite3.Error:
            pass  # unreadable index: rebuild

    total = int(meta.get("content-length") or 0)
    print(f"Downloading and indexing {url} ({total / 1e9:.2f} GB). This takes a few minutes.")
    if os.path.exists(tmp):
        os.remove(tmp)
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript("""
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;
            CREATE TABLE rec(id INTEGER PRIMARY KEY, key TEXT, data TEXT);
            CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
            CREATE VIRTUAL TABLE fts USING fts5(title, authors, venue, year, content='', columnsize=0,
                                                tokenize='unicode61 remove_diacritics 2');
        """)
        with open_dump(url) as resp:
            reader = CountingReader(resp, total)
            with gzip.GzipFile(fileobj=reader) as gz:
                count = index_stream(gz, conn)
        print(file=sys.stderr)
        print("Building key index ...")
        conn.execute("CREATE UNIQUE INDEX rec_key ON rec(key)")
        conn.executemany("INSERT INTO meta VALUES (?, ?)",
                         [("source", remote_id), ("records", str(count)), ("updated", time.strftime("%Y-%m-%d %H:%M:%S"))])
        conn.commit()
        conn.close()
        os.replace(tmp, final)
    except BaseException:
        conn.close()
        if os.path.exists(tmp):
            os.remove(tmp)
        print(file=sys.stderr)
        raise
    print(f"Done: {count:,} records indexed in {final}")
    return 0


# ---------------------------------------------------------------------------
# Searching the local index
# ---------------------------------------------------------------------------

def strip_homonym(name: str) -> str:
    """DBLP disambiguates equal names with a number, e.g. 'Manish Singh 0001'."""
    return re.sub(r"\s+\d{4}$", "", name)


def info_from_record(rec: dict) -> dict:
    f = rec["f"]
    venue = (f.get("journal") or f.get("booktitle") or f.get("series") or f.get("school") or [""])[0]
    people = f.get("author") or f.get("editor") or []
    ee = f.get("ee", [])
    doi = next((e.split("doi.org/", 1)[1] for e in ee if "doi.org/" in e), None)
    info = {
        "key": rec["key"],
        "type": rec["type"],
        "title": f.get("title", [""])[0].rstrip("."),
        "year": (f.get("year") or [""])[0],
        "venue": venue,
        "authors": ", ".join(strip_homonym(p) for p in people),
        "ee": ee[0] if ee else None,
        "rec": rec,
    }
    if doi:
        info["doi"] = doi
    return info


def fts_query(terms) -> str:
    # every term must match (prefix match for the last-typed flexibility); quote to avoid FTS syntax
    parts = []
    for t in terms:
        for word in re.findall(r"\w+", t):
            parts.append('"' + word + '"*')
    return " ".join(parts)


def search_dblp(query: str, max_hits: int = 20):
    path = db_path()
    if not os.path.exists(path):
        print("No local DBLP index found. Run `qbib.py --update` first.", file=sys.stderr)
        raise SystemExit(1)
    q = fts_query([query])
    if not q:
        return []
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT rec.data FROM fts JOIN rec ON rec.id = fts.rowid WHERE fts MATCH ? ORDER BY fts.rank LIMIT ?",
            (q, max_hits)).fetchall()
    finally:
        conn.close()
    return [info_from_record(json.loads(r[0])) for r in rows]


def get_record(key: str):
    conn = sqlite3.connect(db_path())
    try:
        row = conn.execute("SELECT data FROM rec WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    return json.loads(row[0]) if row else None


# ---------------------------------------------------------------------------
# BibTeX generation
# ---------------------------------------------------------------------------

LATEX_ACCENTS = {
    "̀": "`", "́": "'", "̂": "^", "̃": "~", "̈": '"', "̄": "=",
    "̆": "u", "̇": ".", "̊": "r", "̋": "H", "̌": "v", "̧": "c",
    "̨": "k",
}
LATEX_LETTERS = {"ß": r"{\ss}", "ø": r"{\o}", "Ø": r"{\O}", "æ": r"{\ae}", "Æ": r"{\AE}",
                 "œ": r"{\oe}", "Œ": r"{\OE}", "ł": r"{\l}", "Ł": r"{\L}", "đ": r"{\dj}",
                 "Đ": r"{\DJ}", "ı": r"{\i}"}


def to_latex(text: str) -> str:
    out = []
    for ch in unicodedata.normalize("NFD", text):
        if unicodedata.combining(ch):
            acc = LATEX_ACCENTS.get(ch)
            if acc is None or not out:
                continue
            base = out.pop()
            if base in ("i", "j"):
                base = "\\" + base
            if acc.isalpha():
                out.append("{\\" + acc + " " + base + "}")
            else:
                out.append("{\\" + acc + "{" + base + "}}")
        else:
            out.append(LATEX_LETTERS.get(ch, ch))
    s = "".join(out)
    for ch, rep in (("&", r"\&"), ("%", r"\%"), ("#", r"\#"), ("_", r"\_")):
        s = s.replace(ch, rep)
    return s


def bib_type(rec_type: str) -> str:
    return {"data": "misc"}.get(rec_type, rec_type)


def format_entry(key: str, rec_type: str, fields: list) -> str:
    width = max(len(k) for k, _ in fields) if fields else 0
    lines = [f"@{rec_type}{{{key},"]
    for k, v in fields:
        lines.append(f"  {k.ljust(max(width, 12))} = {{{v}}},")
    lines[-1] = lines[-1][:-1]
    lines.append("}")
    return "\n".join(lines)


def record_fields(rec: dict, condensed: bool):
    f = rec["f"]
    fields = []
    for name in BIB_FIELD_ORDER:
        vals = f.get(name)
        if not vals:
            continue
        if name in ("author", "editor"):
            v = " and ".join(to_latex(strip_homonym(p)) for p in vals)
        elif name == "title":
            v = to_latex(vals[0].rstrip("."))
        elif name == "pages":
            v = vals[0].replace("-", "--").replace("----", "--")
        elif name == "booktitle" and condensed and f.get("crossref"):
            v = "{" + to_latex(vals[0]) + "}"
        else:
            v = to_latex(vals[0])
        fields.append((name, v))
    ee = f.get("ee", [])
    doi = next((e.split("doi.org/", 1)[1] for e in ee if "doi.org/" in e), None)
    if doi:
        fields.append(("doi", doi))
    elif ee:
        fields.append(("url", ee[0]))
    arxiv = next((e.rsplit("arxiv.org/abs/", 1)[1] for e in ee if "arxiv.org/abs/" in e), None)
    if arxiv:
        fields += [("eprinttype", "arXiv"), ("eprint", arxiv)]
    fields.append(("biburl", f"https://dblp.org/rec/{rec['key']}.bib"))
    return fields


def get_bibtex_from_info(info: dict, condensed: bool) -> str:
    rec = info["rec"]
    crossref = (rec["f"].get("crossref") or [None])[0]
    fields = record_fields(rec, condensed)
    parent = get_record(crossref) if (condensed and crossref and rec["type"] == "inproceedings") else None
    if parent:
        fields = [(k, v) for k, v in fields if k not in ("publisher", "series", "editor", "volume")]
        fields.insert(len(fields) - 1, ("crossref", "DBLP:" + crossref))
    bibtex = format_entry("DBLP:" + rec["key"], bib_type(rec["type"]), fields)
    if parent:
        pf = [(k, v) for k, v in record_fields(parent, False)]
        bibtex += "\n\n" + format_entry("DBLP:" + parent["key"], bib_type(parent["type"]), pf)
    # apply replacements like Markov to {M}arkov
    for l, r in BIB_REPLACEMENTS:
        bibtex = bibtex.replace(l, r)
    return bibtex


# ---------------------------------------------------------------------------
# Command line interface
# ---------------------------------------------------------------------------

def prettify_html(text: str) -> str:
    return unescape(text.strip())


def get_info_item(info, key):
    res = info.get(key) or ""
    if isinstance(res, list):
        res = ", ".join([r.strip() for r in res])
    return prettify_html(res)


def print_hits(infos):
    current_year = "unkn"

    for i, info in enumerate(infos, start=1):
        title = get_info_item(info, "title")
        year = get_info_item(info, "year")
        year = "unkn" if year == "" else year
        venue = get_info_item(info, "venue")
        authors = get_info_item(info, "authors")

        if year != current_year:
            print(f"\033[1m\033[36m{year}\033[0m")
            current_year = year

        # Line 1: Authors:
        print(f"{i:>2}. {authors}:")

        # Line 2: Title. Venue
        line2 = "    > "
        line2 += f"\033[1m{title}\033[0m"
        if venue == "CoRR":
            venue = "\033[31m" + venue + "\033[0m"
        elif venue == "Zenodo":
            venue = "\033[34m" + venue + "\033[0m"
        elif venue is not None:
            venue = "\033[33m" + venue + "\033[0m"
        if venue is not None:
            line2 += f" {venue}"
        print(line2)

def prompt_choice(n: int) -> int:
    while True:
        try:
            s = input(f"\nSelect an entry [1-{n}] (\033[1mq\033[0muit, \033[1mc\033[0mondensed, \033[1mo\033[0mpen url): ").strip()
        except (KeyboardInterrupt, EOFError):
            s = "q"
        if s in {"0", "q", "quit", "exit"} or "q" in s:
            return 0, False, False
        try:
            condensed = "c" in s
            open_url = "o" in s
            k = int(s.replace("c","").replace("o",""))
            if 1 <= k <= n:
                return k, condensed, open_url
        except ValueError:
            pass
        print(f"Please enter a number between 1 and {n}, or 0 to quit.")

def copy_to_clipboard(text: str):
    if sys.platform == "darwin":
        cmd = ["pbcopy"]
    elif sys.platform.startswith("win"):
        # Windows (PowerShell is available by default on modern Windows)
        cmd = ["powershell", "-NoProfile", "-Command", "Set-Clipboard"]
    else:
        # Linux: prefer Wayland, then X11
        if shutil.which("wl-copy"):
            cmd = ["wl-copy"]
        elif shutil.which("xclip"):
            cmd = ["xclip", "-selection", "clipboard"]
        elif shutil.which("xsel"):
            cmd = ["xsel", "--clipboard", "--input"]
        else:
            raise RuntimeError(
                "No clipboard helper found. Install one of: wl-clipboard (wl-copy), xclip, or xsel."
            )

    subprocess.run(cmd, input=text, text=True, check=True)

def open_url_from_info(info: dict):
    url = None
    if info.get("ee"):
        url = info["ee"]
    elif "doi" in info:
        url = "https://doi.org/" + info["doi"]
    if url is not None:
        # open in new tab
        return webbrowser.open(url, new=2, autoraise=True)

def main():
    parser = argparse.ArgumentParser(description="\033[1m\033[36mqbib\033[0m - Search DBLP and output a selected BibTeX entry.")
    parser.add_argument("terms", nargs="*", help="Search terms")
    parser.add_argument("-n", "--num", type=int, default=20, help="Max results to show (default: 20)")
    parser.add_argument("-u", "--update", action="store_true",
                        help="Download the latest DBLP dump and rebuild the local index (needed once, ~1 GB download)")
    args = parser.parse_args()

    if args.update:
        return update_index()
    if not args.terms:
        parser.error("no search terms given (use --update to build the local index)")

    query = " ".join(args.terms).strip()
    infos = search_dblp(query, max_hits=min(max(args.num, 1), 100))

    if not infos:
        print("No results found.")
        return 0
    # sort by year
    def year_key(info):
        try:
            return int(info["year"] or 0)
        except ValueError:
            return 0  # unknown/non-numeric years go last
    infos.sort(key=year_key, reverse=True)

    print_hits(infos)
    while True:
        choice,condensed,open_url = prompt_choice(len(infos))
        if choice == 0:
            return 0

        info = infos[choice - 1]
        bibtex = get_bibtex_from_info(info, condensed)

        HEADING= "\n\033[1m\033[36m" + ("-"*32) + " DBLP  STANDARD " + ("-"*32) + "\033[0m\n"
        if condensed:
            HEADING = HEADING.replace(" STANDARD", "CONDENSED")
        SEPARATOR= "\n\033[1m\033[36m" + ("-"*80) + "\033[0m\n"
        copy_to_clipboard(bibtex)
        print(HEADING + bibtex + SEPARATOR + "\033[36mCopied to clipboard.\033[0m")
        if (open_url):
            open_url_from_info(info)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
