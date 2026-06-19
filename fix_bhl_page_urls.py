"""
Backfill correct BHL protologue page URLs into fotw_ipni_bhl.csv.

Background
----------
The first version of mine_ipni_bhl.py pulled `bhl_page_id` from
`linkedPublication.bhlPageLink`, which is a *publication-level* link (BHL's
landing page for the journal/book). The true protologue link is in IPNI's
top-level `bhlLink` field — an OpenURL string that encodes the volume and
page where the species was first described:

  http://www.biodiversitylibrary.org/openurl?ctx_ver=Z39.88-2004
    &rft.date=1753 &rft.volume=2 &rft.spage=951
    &rft_id=http://www.biodiversitylibrary.org/bibliography/669
    &rft_val_fmt=info:ofi/fmt:kev:mtx:book &url_ver=z39.88-2004

BHL's OpenURL resolver redirects this to a clean /page/{id} URL that lands
on the exact protologue page. We don't need to re-query IPNI to fix this —
the OpenURL is a deterministic template, and the fields we need are already
in fotw_ipni_bhl.csv:

  rft.date    ← protologue_year
  rft.volume  ← volume part of protologue_collation
  rft.spage   ← page  part of protologue_collation
  rft_id      ← https://www.biodiversitylibrary.org/bibliography/{bhl_title_id}

For each row with all three, we reconstruct the OpenURL, follow the
redirect to capture the resolved /page/{id} URL, and update bhl_page_id +
bhl_page_url in place. Rows where the OpenURL can't be built or BHL fails
to resolve to a clean /page/ URL get their bhl_page_id and bhl_page_url
left blank — same convention as before.

Usage
-----
    python3 fix_bhl_page_urls.py
"""

import csv
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen, HTTPRedirectHandler, build_opener

csv.field_size_limit(sys.maxsize)

BASE     = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE, "fotw_ipni_bhl.csv")
LOG_PATH = os.path.join(BASE, "fix_bhl_page_urls.log")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; FotW-IPNI-research-bot/1.0; "
        "Boise State University; contact: sven.buerki@boisestate.edu)"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

OPENURL_TEMPLATE = (
    "https://www.biodiversitylibrary.org/openurl"
    "?ctx_ver=Z39.88-2004"
    "&rft.date={year}"
    "&rft.volume={volume}"
    "&rft.spage={spage}"
    "&rft_id=https://www.biodiversitylibrary.org/bibliography/{title_id}"
    "&rft_val_fmt=info:ofi/fmt:kev:mtx:book"
    "&url_ver=z39.88-2004"
)

# Match the most common collation formats: "Vol: Page" possibly with junk
# after. Examples that should parse:
#   "2: 951"                → 2 / 951
#   "62: 147"               → 62 / 147
#   "ser. 2, 24(2): 253"    → 24 / 253   (we accept the LAST vol:page pair)
#   "13: 90, t. 12"         → 13 / 90
COLLATION_RE = re.compile(r"(\d+)\s*\(\s*\d+\s*\)?\s*:\s*(\d+)|(\d+)\s*:\s*(\d+)")

PAGE_RE = re.compile(r"biodiversitylibrary\.org/page/(\d+)")


def parse_collation(s):
    """Return (volume, page) as strings, or (None, None) if not parseable."""
    if not s:
        return None, None
    # Use the LAST vol:page pair in the string (often the most specific).
    m = None
    for m in COLLATION_RE.finditer(s):
        pass
    if m is None:
        return None, None
    vol  = m.group(1) or m.group(3)
    page = m.group(2) or m.group(4)
    return vol, page


def build_openurl(year, volume, spage, title_id):
    return OPENURL_TEMPLATE.format(
        year=quote(str(year)),
        volume=quote(str(volume)),
        spage=quote(str(spage)),
        title_id=quote(str(title_id)),
    )


def resolve_to_page_id(openurl):
    """
    Open the OpenURL and follow redirects; return the resolved /page/{id}
    if BHL lands on one, else "".
    """
    try:
        req = Request(openurl, headers=HEADERS, method="GET")
        with urlopen(req, timeout=20) as resp:
            final_url = resp.geturl()
            m = PAGE_RE.search(final_url)
            return m.group(1) if m else ""
    except HTTPError as e:
        # BHL sometimes returns 403 on the final HTML body even after a
        # successful redirect — capture the URL that we were redirected to.
        url = getattr(e, "url", "") or ""
        m = PAGE_RE.search(url)
        return m.group(1) if m else ""
    except (URLError, Exception):
        return ""


def fix_row(row, delay):
    """Returns (new_page_id, new_page_url, log_msg). Mutates nothing."""
    time.sleep(delay)
    year     = (row.get("protologue_year") or "").strip()
    title_id = (row.get("bhl_title_id")    or "").strip()
    coll     = (row.get("protologue_collation") or "").strip()

    if not (year and title_id and coll):
        return "", "", "skip:missing_fields"

    volume, spage = parse_collation(coll)
    if not (volume and spage):
        return "", "", f"skip:unparseable_collation:{coll}"

    openurl = build_openurl(year, volume, spage, title_id)
    page_id = resolve_to_page_id(openurl)
    if not page_id:
        return "", "", f"skip:no_page_redirect:{openurl}"

    page_url = f"https://www.biodiversitylibrary.org/page/{page_id}"
    return page_id, page_url, "ok"


def main():
    print(f"Reading {os.path.basename(CSV_PATH)} …")
    with open(CSV_PATH, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fields = reader.fieldnames
        rows = list(reader)
    print(f"  {len(rows):,} rows")

    n_with_year = sum(1 for r in rows if r.get("protologue_year"))
    n_with_title = sum(1 for r in rows if r.get("bhl_title_id"))
    n_with_coll = sum(1 for r in rows if r.get("protologue_collation"))
    print(f"  with protologue_year:        {n_with_year:,}")
    print(f"  with bhl_title_id:           {n_with_title:,}")
    print(f"  with protologue_collation:   {n_with_coll:,}")

    # Candidates: rows with all three
    todo = [i for i, r in enumerate(rows)
            if r.get("protologue_year") and r.get("bhl_title_id")
            and r.get("protologue_collation")]
    print(f"\n  Candidate rows (all 3 fields present): {len(todo):,}\n")

    log = open(LOG_PATH, "w", encoding="utf-8")
    log.write("taxonID\tscientificName\told_page_id\tnew_page_id\tstatus\n")

    n_ok = n_skip_unparse = n_skip_no_redir = n_changed = n_unchanged = 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fix_row, rows[i], 0.05): i for i in todo}
        for j, future in enumerate(as_completed(futures), 1):
            i = futures[future]
            new_pid, new_purl, msg = future.result()
            old_pid = rows[i].get("bhl_page_id", "")

            if msg == "ok":
                n_ok += 1
                if new_pid != old_pid:
                    n_changed += 1
                else:
                    n_unchanged += 1
                rows[i]["bhl_page_id"]  = new_pid
                rows[i]["bhl_page_url"] = new_purl
                log.write(f"{rows[i]['taxonID']}\t{rows[i]['scientificName']}\t{old_pid}\t{new_pid}\tok\n")
            elif msg.startswith("skip:unparseable"):
                n_skip_unparse += 1
                # Wipe the old (wrong) page link — it was definitely bogus.
                rows[i]["bhl_page_id"]  = ""
                rows[i]["bhl_page_url"] = ""
                log.write(f"{rows[i]['taxonID']}\t{rows[i]['scientificName']}\t{old_pid}\t\t{msg}\n")
            elif msg.startswith("skip:no_page_redirect"):
                n_skip_no_redir += 1
                rows[i]["bhl_page_id"]  = ""
                rows[i]["bhl_page_url"] = ""
                log.write(f"{rows[i]['taxonID']}\t{rows[i]['scientificName']}\t{old_pid}\t\t{msg}\n")

            elapsed = time.time() - t0
            rate = j / elapsed if elapsed else 0
            eta  = (len(todo) - j) / rate if rate else 0
            print(f"  [{j:>6}/{len(todo)}]  ok={n_ok}  changed={n_changed}  "
                  f"no_redir={n_skip_no_redir}  unparse={n_skip_unparse}  "
                  f"|  ETA {eta/60:.1f}min", end="\r", flush=True)

    # Also: for non-candidate rows (missing fields), wipe stale page links so
    # we don't keep the wrong-from-the-start /page/ urls in the file.
    n_wiped_noncandidate = 0
    candidate_set = set(todo)
    for i, r in enumerate(rows):
        if i in candidate_set:
            continue
        if r.get("bhl_page_id"):
            n_wiped_noncandidate += 1
            r["bhl_page_id"]  = ""
            r["bhl_page_url"] = ""

    log.close()

    print("\n\n" + "=" * 65)
    print("Done.")
    print(f"  Candidates processed         : {len(todo):,}")
    print(f"  Resolved successfully        : {n_ok:,}")
    print(f"    of which IDs changed       : {n_changed:,}")
    print(f"    of which IDs unchanged     : {n_unchanged:,}")
    print(f"  No /page/ in redirect        : {n_skip_no_redir:,}")
    print(f"  Unparseable collation        : {n_skip_unparse:,}")
    print(f"  Non-candidate stale wiped    : {n_wiped_noncandidate:,}")
    print(f"  Log file                     : {LOG_PATH}")

    print(f"\nWriting corrected {os.path.basename(CSV_PATH)} …")
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print("Done.")


if __name__ == "__main__":
    main()
