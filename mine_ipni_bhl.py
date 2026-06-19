"""
Mine IPNI for protologue metadata + BHL scan links for every FotW taxon.

For each FotW taxon in ../FotW_DB/fotw_taxonomy_resolved.csv:
  Query IPNI's search API for the taxon's scientific name
  Pick the best-matching record (exact name + author when available; otherwise
  prefer topCopy or hasOriginalData)
  Extract: IPNI id, canonical name, author, protologue reference, year,
  publication title, BHL page link, BHL title link

Why IPNI rather than BHL's NameSearch?
  IPNI is the authoritative source for plant nomenclature. Every IPNI record
  carries the protologue citation (where the name was first validly published)
  and, for ~75% of records, a direct OpenURL link to the scanned page on BHL
  (`bhlLink`). BHL's own NameSearch returns OCR-detected mentions across the
  whole BHL corpus — useful, but it does not distinguish the protologue from
  the hundreds of incidental mentions in floras and monographs.

API
---
  IPNI: https://www.ipni.org/api/1/search        (free, no key)
        https://www.ipni.org/api/1/n/{ipni_id}   (full record)
  BHL link inside IPNI records is an OpenURL — no extra BHL call needed.

Usage
-----
    python3 mine_ipni_bhl.py                # all 12,838 FotW taxa
    python3 mine_ipni_bhl.py --limit 50     # pilot
    python3 mine_ipni_bhl.py --workers 6 --delay 0.3

Resumable: already-mined taxonIDs are skipped on re-run.
"""

import argparse
import csv
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse, parse_qs
from urllib.request import Request, urlopen

csv.field_size_limit(sys.maxsize)

BASE          = os.path.dirname(os.path.abspath(__file__))
FOTW_CSV      = os.path.normpath(os.path.join(BASE, "..", "FotW_DB", "fotw_taxonomy_resolved.csv"))
OUT_CSV       = os.path.join(BASE, "fotw_ipni_bhl.csv")
LOG_FILE      = os.path.join(BASE, "mine_ipni_bhl.log")

IPNI_API      = "https://www.ipni.org/api/1/search"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; FotW-IPNI-research-bot/1.0; "
        "Boise State University; contact: sven.buerki@boisestate.edu)"
    ),
    "Accept": "application/json",
}

OUT_FIELDS = [
    "taxonID", "scientificName",
    "accepted_gbif_id", "accepted_gbif_url",
    "ipni_id", "ipni_url",
    "ipni_name", "ipni_author",
    "match_quality",
    # Protologue
    "protologue_reference",        # e.g. "Sp. Pl. 2: 951. 1753"
    "protologue_year",
    "protologue_publication",      # journal/book title abbrev
    "protologue_collation",        # volume:page
    # BHL scan links to the protologue
    "bhl_page_id",
    "bhl_page_url",
    "bhl_title_id",
    "bhl_title_url",
    # Type + distribution (from IPNI name record)
    "type_note",                   # typification reference, e.g. "Dodoens, Stirp. Hist. ..."
    "distribution",                # free-text geographic note from IPNI
    # Cross-database identifiers
    "wfo_id",                      # World Flora Online ID
    "wfo_url",                     # https://www.worldfloraonline.org/taxon/{wfo_id}
    "in_powo",                     # "y"/"n" — whether POWO has a profile
    "powo_url",                    # POWO taxon page URL (only when in_powo=y)
    # Quality signal — IPNI duplicate-record count
    "n_same_citation_as",
    "error",
]


# ── IPNI calls ────────────────────────────────────────────────────────────────
def fetch_json(url):
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


# Strip the author from a FotW name to get just "Genus epithet" or
# "Genus epithet infraepithet". Author starts at first uppercase token that
# isn't a recognised infraspecific marker.
INFRA_MARKERS = {"subsp.", "var.", "f.", "subsp", "var", "f", "ssp.", "ssp", "cv."}
def split_name_author(full):
    """
    Return (canonical_name, author_str). The canonical part keeps genus +
    epithet (+ infraspecific marker + infraspecific epithet) and drops any
    trailing author citation.
    """
    if not full:
        return "", ""
    tokens = full.strip().split()
    canon = []
    i = 0
    # Genus
    if i < len(tokens):
        canon.append(tokens[i]); i += 1
    # Epithet (lowercase, possibly hybrid marker × before genus handled by user)
    if i < len(tokens) and tokens[i] and tokens[i][0].islower():
        canon.append(tokens[i]); i += 1
    # Optional infraspecific (subsp./var./f. + epithet)
    while i < len(tokens) and tokens[i] in INFRA_MARKERS and i+1 < len(tokens):
        canon.append(tokens[i]); canon.append(tokens[i+1]); i += 2
    author = " ".join(tokens[i:])
    return " ".join(canon), author.strip()


def author_match(ipni_author, fotw_author):
    """Loose author match: trim, lowercase, drop dots and ampersand variants."""
    def norm(a):
        a = (a or "").lower().replace(".", "").replace("&", " and ")
        return re.sub(r"\s+", " ", a).strip()
    return bool(fotw_author) and norm(ipni_author) == norm(fotw_author)


def search_ipni(canonical_name):
    """Return the list of IPNI hits for the canonical (Genus + epithet) query."""
    url = f"{IPNI_API}?perPage=20&q={quote(canonical_name)}"
    data = fetch_json(url)
    return data.get("results", []) or []


def choose_best(results, canonical_name, fotw_author):
    """
    Pick the best IPNI record for our FotW name.
    Priority:
      1) exact canonical name + exact author match
      2) exact canonical name + topCopy True
      3) exact canonical name (any)
      4) first hit (fallback)
    """
    exact = [r for r in results if (r.get("name") or "").strip().lower() == canonical_name.strip().lower()]
    pool  = exact or results
    if not pool:
        return None, "no_match"

    if fotw_author:
        for r in pool:
            if author_match(r.get("authors", ""), fotw_author):
                return r, "exact_with_author"

    for r in pool:
        if r.get("topCopy") is True:
            return r, "exact_top_copy" if exact else "fuzzy_top_copy"

    return pool[0], ("exact_canonical" if exact else "fuzzy_canonical")


# ── BHL link parsing ──────────────────────────────────────────────────────────
# IPNI's bhlLink/bhlTitleLink/bhlPageLink are OpenURL strings that embed a real
# BHL URL inside rft_id. Extract the embedded page ID or bibliography ID.
PAGE_RE  = re.compile(r"biodiversitylibrary\.org/page/(\d+)")
TITLE_RE = re.compile(r"biodiversitylibrary\.org/bibliography/(\d+)")

def parse_bhl(record):
    """
    Pull (bhl_page_id, bhl_page_url, bhl_title_id, bhl_title_url) from an
    IPNI name record.

    Important: the top-level `bhlLink` field is an OpenURL with rft.volume +
    rft.spage that resolves to the protologue page. The nested
    `linkedPublication.bhlPageLink` is a PUBLICATION-level link (BHL's
    landing page for the journal/book) and must NOT be treated as the
    protologue page. We resolve the top-level OpenURL via a redirect to get
    the actual `/page/{id}` URL where the species was first described.

    The title link comes from `linkedPublication.bhlTitleLink` (or the
    rft_id fragment of any /bibliography/{id} URL we encounter).
    """
    page_id = page_url = title_id = title_url = ""

    # Title link: any /bibliography/{id} URL we see in any of the OpenURL fields.
    lp = record.get("linkedPublication") or {}
    for c in (record.get("bhlLink", ""),
              lp.get("bhlTitleLink", ""),
              lp.get("bhlPageLink", "")):
        if not c:
            continue
        m = TITLE_RE.search(c)
        if m and not title_id:
            title_id = m.group(1)
            title_url = f"https://www.biodiversitylibrary.org/bibliography/{title_id}"
            break

    # Protologue page link: ONLY the top-level bhlLink is reliable here, and
    # only after we resolve the OpenURL through BHL's redirect. We do that
    # lookup lazily in `_resolve_bhl_page` so callers can disable it for
    # offline / dry-run modes.
    page_id, page_url = _resolve_bhl_page(record.get("bhlLink", ""))

    return page_id, page_url, title_id, title_url


def _resolve_bhl_page(openurl):
    """
    Follow BHL's OpenURL redirect to capture the resolved /page/{id} URL.
    Returns ("", "") if the OpenURL is empty or the redirect doesn't land
    on a page-level URL.
    """
    if not openurl:
        return "", ""
    # Normalise to https.
    openurl = openurl.replace("http://", "https://", 1)
    try:
        req = Request(openurl, headers=HEADERS, method="GET")
        with urlopen(req, timeout=20) as resp:
            final = resp.geturl()
    except HTTPError as e:
        final = getattr(e, "url", "") or ""
    except (URLError, Exception):
        return "", ""
    m = PAGE_RE.search(final)
    if not m:
        return "", ""
    page_id = m.group(1)
    return page_id, f"https://www.biodiversitylibrary.org/page/{page_id}"


# ── Worker ────────────────────────────────────────────────────────────────────
def mine_one(row, delay):
    time.sleep(delay)
    canonical, fotw_author = split_name_author(row["scientificName"])
    if not canonical:
        return _empty(row, "blank_name", "")
    try:
        results = search_ipni(canonical)
    except Exception as exc:
        return _empty(row, "", str(exc))

    chosen, quality = choose_best(results, canonical, fotw_author)
    if chosen is None:
        return _empty(row, "no_match", "")

    page_id, page_url, title_id, title_url = parse_bhl(chosen)
    lp = chosen.get("linkedPublication") or {}
    ipni_id = _extract_id(chosen)

    same_as = chosen.get("sameCitationAs") or []

    in_powo = "y" if chosen.get("inPowo") else "n"
    powo_url = (
        f"https://powo.science.kew.org/taxon/urn:lsid:ipni.org:names:{ipni_id}"
        if (in_powo == "y" and ipni_id) else ""
    )

    wfo_id = chosen.get("wfoId", "") or ""
    wfo_url = f"https://www.worldfloraonline.org/taxon/{wfo_id}" if wfo_id else ""

    return {
        "taxonID"               : row["taxonID"],
        "scientificName"        : row["scientificName"],
        "accepted_gbif_id"      : row.get("accepted_gbif_id", ""),
        "accepted_gbif_url"     : row.get("accepted_gbif_url", ""),
        "ipni_id"               : ipni_id,
        "ipni_url"              : f"https://www.ipni.org/n/{ipni_id}" if ipni_id else "",
        "ipni_name"             : chosen.get("name", ""),
        "ipni_author"           : chosen.get("authors", ""),
        "match_quality"         : quality,
        "protologue_reference"  : chosen.get("reference", ""),
        "protologue_year"       : str(chosen.get("publicationYear", "") or ""),
        "protologue_publication": lp.get("title", "") or chosen.get("publication", ""),
        "protologue_collation"  : chosen.get("referenceCollation", ""),
        "bhl_page_id"           : page_id,
        "bhl_page_url"          : page_url,
        "bhl_title_id"          : title_id,
        "bhl_title_url"         : title_url,
        "type_note"             : chosen.get("typeNote", "") or "",
        "distribution"          : chosen.get("distribution", "") or "",
        "wfo_id"                : wfo_id,
        "wfo_url"               : wfo_url,
        "in_powo"               : in_powo,
        "powo_url"              : powo_url,
        "n_same_citation_as"    : str(len(same_as)),
        "error"                 : "",
    }


def _extract_id(rec):
    """Pull a clean IPNI ID from a record's fqId or url field."""
    if not rec:
        return ""
    fq = rec.get("fqId", "") or ""
    if "urn:lsid:ipni.org:" in fq:
        return fq.split(":")[-1]
    u = (rec.get("url", "") or "").strip()
    if u.startswith("/n/") or u.startswith("/a/") or u.startswith("/p/"):
        return u[3:]
    return ""


def _empty(row, quality, err):
    return {f: "" for f in OUT_FIELDS} | {
        "taxonID"          : row["taxonID"],
        "scientificName"   : row["scientificName"],
        "accepted_gbif_id" : row.get("accepted_gbif_id", ""),
        "accepted_gbif_url": row.get("accepted_gbif_url", ""),
        "match_quality"    : quality,
        "error"            : err,
    }


def _clean_strings(d):
    """Unescape HTML entities in every string value of the output dict."""
    return {k: (html.unescape(v).strip() if isinstance(v, str) else v) for k, v in d.items()}


# ── Resume / IO ───────────────────────────────────────────────────────────────
def load_done(out_csv):
    done = set()
    if os.path.exists(out_csv):
        with open(out_csv, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                done.add(row["taxonID"])
    return done


def load_taxa(fotw_csv, limit=None):
    rows = []
    with open(fotw_csv, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            tid = (r.get("taxonID") or "").strip()
            name = (r.get("scientificName") or "").strip()
            if not tid or not name:
                continue
            gbif = (r.get("accepted_gbif_id") or "").strip()
            rows.append({
                "taxonID": tid,
                "scientificName": name,
                "accepted_gbif_id": gbif,
                "accepted_gbif_url": f"https://gbif.org/species/{gbif}" if gbif else "",
            })
            if limit and len(rows) >= limit:
                break
    return rows


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--workers", type=int, default=6,
                        help="Concurrent workers (default: 6)")
    parser.add_argument("--delay", type=float, default=0.3,
                        help="Pause per worker between requests (default: 0.3s)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only mine the first N taxa (pilot use)")
    args = parser.parse_args()

    print(f"Loading FotW taxa from {os.path.basename(FOTW_CSV)} …")
    taxa = load_taxa(FOTW_CSV, limit=args.limit)
    print(f"  {len(taxa):,} taxa in scope"
          + (f" (limited to {args.limit})" if args.limit else ""))

    print("Checking for previous run …")
    done = load_done(OUT_CSV)
    todo = [t for t in taxa if t["taxonID"] not in done]
    print(f"  Already mined: {len(done):,}  |  Remaining: {len(todo):,}")

    est_min = len(todo) * args.delay / max(args.workers, 1) / 60
    print(f"  Estimated time: ~{est_min:.0f} min ({args.workers} workers, {args.delay}s delay)\n")

    is_new = not os.path.exists(OUT_CSV)
    out_fh = open(OUT_CSV, "a", newline="", encoding="utf-8")
    log_fh = open(LOG_FILE, "a", encoding="utf-8")
    writer = csv.DictWriter(out_fh, fieldnames=OUT_FIELDS, extrasaction="ignore")
    if is_new:
        writer.writeheader()

    total = len(todo)
    done_count = errors = with_ipni = with_bhl_page = with_bhl_title = 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(mine_one, row, args.delay): row for row in todo}
        for future in as_completed(futures):
            result = future.result()
            done_count += 1
            if result["error"]:
                errors += 1
                log_fh.write(f"{result['taxonID']}\t{result['scientificName']}\tERROR\t{result['error']}\n")
                log_fh.flush()
            if result["ipni_id"]:
                with_ipni += 1
            if result["bhl_page_url"]:
                with_bhl_page += 1
            if result["bhl_title_url"]:
                with_bhl_title += 1
            writer.writerow(_clean_strings(result))
            out_fh.flush()

            elapsed = time.time() - t0
            rate = done_count / elapsed if elapsed else 0
            eta = (total - done_count) / rate if rate else 0
            name_disp = result["scientificName"][:40]
            flag = (" [ERR]"  if result["error"] else
                    " [BHL]"  if result["bhl_page_url"] else
                    " [IPNI]" if result["ipni_id"] else
                    " [-]")
            print(
                f"  [{done_count:>6}/{total}]  {name_disp:<42}  "
                f"{result['match_quality']:<22}{flag}  |  ETA {eta/60:.1f}min",
                end="\r", flush=True,
            )

    out_fh.close()
    log_fh.close()

    print(f"\n\n{'='*65}")
    print("Done.")
    print(f"  Mined this run                : {done_count:,}")
    print(f"  IPNI ID found                 : {with_ipni:,}  ({100*with_ipni/max(done_count,1):.1f}%)")
    print(f"  BHL protologue page URL found : {with_bhl_page:,}  ({100*with_bhl_page/max(done_count,1):.1f}%)")
    print(f"  BHL title URL found           : {with_bhl_title:,}  ({100*with_bhl_title/max(done_count,1):.1f}%)")
    print(f"  Errors                        : {errors:,}")
    print(f"  Output CSV                    : {OUT_CSV}")
    print(f"  Error log                     : {LOG_FILE}")


if __name__ == "__main__":
    main()
