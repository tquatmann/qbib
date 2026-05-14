#!/usr/bin/env python3
import argparse
import json
import sys
import time
import shutil
import subprocess
import webbrowser
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from collections import OrderedDict
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

BIB_UNWANTED_KEYS = ["biburl","bibsource","timestamp"]

# DBLP Mirrors
DEFAULT_BASES = [
    "https://dblp.uni-trier.de",
    "https://dblp.dagstuhl.de",
    "https://dblp.org",
]

SEARCH_PATH = "/search/publ/api"
REC_PATH_PREFIX = "/rec/"

def http_get_text(urlsuffix: str, timeout: float = 20.0, retries: int = 4) -> str:
    last_err = None
    for attempt in range(retries):
        base = DEFAULT_BASES[attempt % len(DEFAULT_BASES)]
        url = base + urlsuffix
        if attempt > 0:
            print("API request failed. Next attempt\n\t{}".format(url))
        try:
            req = urllib.request.Request(
                url,
                headers={
                    # A descriptive UA helps; DBLP may throttle/deny ambiguous clients.
                    "User-Agent": "dblp-bibtex-cli/1.1 (mailto:you@example.com)",
                    "Accept": "*/*",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as e:
            # Read body for diagnostics; then maybe retry on 5xx
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            last_err = RuntimeError(f"HTTP Error {e.code} for {url}\n{body[:800]}")
            if 500 <= e.code < 600 and attempt < retries - 1:
                time.sleep(0.8 * (2 ** attempt))
                continue
            raise last_err
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(0.8 * (2 ** attempt))
                continue
            raise
    raise last_err or RuntimeError("Request failed")


def normalize_authors_json(authors_field) -> str:
    if not authors_field:
        return ""
    a = authors_field.get("author")
    if not a:
        return ""
    if isinstance(a, list):
        out = []
        for item in a:
            if isinstance(item, dict) and "@text" in item:
                out.append(item["@text"])
            else:
                out.append(str(item))
        return ", ".join(out)
    if isinstance(a, dict) and "@text" in a:
        return a["@text"]
    return str(a)


def search_dblp_json(query: str, max_hits: int = 20):
    params = {"q": query, "format": "json", "h": str(max_hits)}
    url = SEARCH_PATH + "?" + urllib.parse.urlencode(params)
    raw = http_get_text(url)
    data = json.loads(raw)
    hits = data.get("result", {}).get("hits", {}).get("hit", [])
    if isinstance(hits, dict):
        hits = [hits]
    # unify shape to match the rest of the code (hit["info"] dict)
    return [h.get("info", {}) for h in hits]


def search_dblp_xml(query: str, max_hits: int = 20):
    params = {"q": query, "format": "xml", "h": str(max_hits)}
    url = SEARCH_PATH + "?" + urllib.parse.urlencode(params)
    raw = http_get_text(url)
    root = ET.fromstring(raw)

    infos = []
    for hit in root.findall(".//hit"):
        info = hit.find("info")
        if info is None:
            continue
        def t(tag): return (info.findtext(tag) or "").strip()
        authors = [a.text.strip() for a in info.findall("./authors/author") if a.text]
        infos.append({
            "title": t("title"),
            "year": t("year"),
            "venue": t("venue"),
            "url": t("url"),
            "key": t("key"),
            "_authors_str": ", ".join(authors),
        })
    return infos


def search_dblp(query: str, max_hits: int = 20):
    # Try JSON first; if DBLP errors, fall back to XML.
    try:
        infos = search_dblp_json(query, max_hits=max_hits)
        # annotate authors string for printing convenience
        for info in infos:
            info["_authors_str"] = normalize_authors_json(info.get("authors", {}))
        return infos
    except Exception:
        return search_dblp_xml(query, max_hits=max_hits)


def bibtex_url_from_info(info: dict, condensed: bool) -> str:
    key = (info.get("key") or "").strip()
    if key:
        param = 0 if condensed else 1
        return f"{REC_PATH_PREFIX}{key}.bib?param={param}"
    rec_url = (info.get("url") or "").strip()
    if rec_url.endswith(".html"):
        return rec_url[:-5] + ".bib"
    if rec_url and not rec_url.endswith(".bib"):
        return rec_url + ".bib"
    return rec_url

def bibtex_has_key(bibtex: str, key: str):
    return any([l.strip().startswith(key) for l in bibtex.split("\n")])

def get_bibtex_from_info(info: dict, condensed: bool):
    bib_url = bibtex_url_from_info(info, condensed)
    if not bib_url:
        print("Could not determine BibTeX URL for the selected entry.", file=sys.stderr)
    bibtex = http_get_text(bib_url).strip()
    # apply replacements like Markov to {M}arkov
    for l,r in BIB_REPLACEMENTS:     
        bibtex = bibtex.replace(l,r)
    # ensure that we have a url or a doi
    have_doi = bibtex_has_key(bibtex, "doi")
    have_url = bibtex_has_key(bibtex, "url")
    bibtex_lines = bibtex.split("\n")[:-1]
    bibtex_lines[-1] += "," # ensure that we always end with a comma
    if not have_doi and not have_url:
        num_white = bibtex_lines[1].find("=")
        if "doi" in info:
            new_line = "  doi{}= {{{}}},".format(" " * (num_white - 5), info["doi"])
            bibtex_lines.append(new_line)
        elif "ee" in info:
            new_line = "  url{}= {{{}}},".format(" " * (num_white - 5), info["ee"])
            bibtex_lines.append(new_line)
    # filter lines we don't need
    unwanted_keys = BIB_UNWANTED_KEYS
    if have_doi and have_url: # no need for both
        unwanted_keys.append("url")
    def keep(line):
        return not any([ line.strip().startswith(f) for f in BIB_UNWANTED_KEYS])
    bibtex = "\n".join([l for l in bibtex_lines if keep(l)]).strip()
    bibtex = bibtex[:-1] + "\n}" # cut away last comma and add closing bracket
    return bibtex
    
def prettify_html(text : str) -> str:
    return unescape(text.strip())
    
def normalize_authors_json(authors_field) -> str:
    """
    Handles DBLP JSON author formats, e.g.
      {"authors": {"author": [{"@pid": "...", "text": "A"}, ...]}}
      {"authors": {"author": {"@pid": "...", "text": "A"}}}
    """
    if not authors_field:
        return ""

    # authors_field is usually a dict {"author": ...}
    a = authors_field.get("author") if isinstance(authors_field, dict) else authors_field
    if not a:
        return ""

    def name_of(item) -> str:
        if isinstance(item, str):
            return item.strip()
        if isinstance(item, dict):
            return (item.get("@text") or item.get("text") or item.get("#text") or "").strip()
        return str(item).strip()

    if isinstance(a, list):
        names = [name_of(x) for x in a]
    else:
        names = [name_of(a)]

    names = [prettify_html(n) for n in names if n]
    return ", ".join(names)

def get_info_item(info, key):
    res = info.get(key) or ""
    if isinstance(res, list):
        res = ", ".join([r.strip() for r in res])
    return prettify_html(res)

def print_hits(infos):
    width = shutil.get_terminal_size((120, 20)).columns
    current_year = "unkn"
    
    for i, info in enumerate(infos, start=1):
        title = get_info_item(info, "title")
        year = get_info_item(info, "year")
        year = "unkn" if  year == "" else year
        venue = get_info_item(info, "venue")
        authors = normalize_authors_json((info.get("_authors_str") or "").strip())
        
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
        except KeyboardInterrupt:
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
    if "ee" in info:
        url = info["ee"]
    elif "doi" in info:
        url = "https://doi/org/" + info["doi"]
    if url is not None:
        # open in new tab
        return webbrowser.open(url, new=2, autoraise=True)
    
def main():
    parser = argparse.ArgumentParser(description="\033[1m\033[36mqbib\033[0m - Search DBLP and output a selected BibTeX entry.")
    parser.add_argument("terms", nargs="+", help="Search terms")
    parser.add_argument("-n", "--num", type=int, default=20, help="Max results to show (default: 20)")
    args = parser.parse_args()

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