"""
Retry IPNI lookup for rows in fotw_ipni_bhl.csv that have a low match_quality
(no_match / fuzzy_top_copy / fuzzy_canonical), using two extra strategies:

  1. Autonym stripping  — "Croton gratissimus var. gratissimus" → "Croton gratissimus"
     (IPNI doesn't store autonyms as separate records.)
  2. Gender swap        — "Aleurites moluccana" (feminine) → "Aleurites moluccanus"
                                                          → "Aleurites moluccanum"
     (Latin gender agreement varies between original and current names.)

Updates rows in place. The suffix "_autonym" or "_gender" is appended to
match_quality on successful retries so the source of the fix is traceable.

Usage
-----
    python3 retry_low_quality.py
"""

import csv
import html
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# Reuse the helpers + schema from the main miner
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mine_ipni_bhl import (
    OUT_FIELDS, split_name_author, INFRA_MARKERS,
    search_ipni, choose_best, parse_bhl, _extract_id, _clean_strings,
)

csv.field_size_limit(sys.maxsize)

BASE    = os.path.dirname(os.path.abspath(__file__))
OUT_CSV = os.path.join(BASE, "fotw_ipni_bhl.csv")
LOG_FILE = os.path.join(BASE, "retry_low_quality.log")

RETRY_TARGETS = {"no_match", "fuzzy_top_copy", "fuzzy_canonical"}

# Higher index = better quality
QUALITY_RANK = [
    "blank_name", "no_match",
    "fuzzy_canonical", "fuzzy_top_copy",
    "exact_canonical", "exact_top_copy", "exact_with_author",
]
def is_better(new_q, old_q):
    new_base = new_q.split("_autonym")[0].split("_gender")[0]
    old_base = old_q.split("_autonym")[0].split("_gender")[0]
    return QUALITY_RANK.index(new_base) > QUALITY_RANK.index(old_base)


# ── Alt-name strategies ──────────────────────────────────────────────────────
GENDER_ENDINGS = ["a", "us", "um"]

def strip_autonym(canonical):
    """
    Drop a redundant infraspecific autonym, e.g.
      "Croton gratissimus var. gratissimus" → "Croton gratissimus"
    Returns the stripped form, or None if not an autonym.
    """
    tokens = canonical.split()
    # Walk through positions where an infra marker could sit (needs a token
    # before it and after it).
    for i in range(1, len(tokens) - 1):
        if tokens[i] in INFRA_MARKERS and tokens[i-1] == tokens[i+1]:
            return " ".join(tokens[:i])
    return None


def gender_swap_variants(canonical):
    """
    For 'Genus epithet' where epithet ends in -a / -us / -um, return up to two
    alternative spellings with the other endings.
    """
    tokens = canonical.split()
    if len(tokens) != 2:
        return []
    epithet = tokens[1]
    for end in GENDER_ENDINGS:
        if epithet.endswith(end):
            stem = epithet[: -len(end)]
            alts = [stem + e for e in GENDER_ENDINGS if e != end]
            return [f"{tokens[0]} {a}" for a in alts]
    return []


# ── Worker ────────────────────────────────────────────────────────────────────
def retry_one(row, delay=0.3):
    """
    Try improving the IPNI match for a single existing row. Returns (new_row,
    strategy_used) or (None, None) if no improvement found.
    """
    canonical, fotw_author = split_name_author(row["scientificName"])
    if not canonical:
        return None, None

    current_quality = row["match_quality"]

    # Build strategy list (label, alt_canonical)
    strategies = []
    auto = strip_autonym(canonical)
    if auto:
        strategies.append(("autonym", auto))
    for v in gender_swap_variants(canonical):
        strategies.append(("gender", v))

    best_row, best_label = None, None
    for label, alt in strategies:
        time.sleep(delay)
        try:
            results = search_ipni(alt)
        except Exception:
            continue
        if not results:
            continue
        chosen, quality = choose_best(results, alt, fotw_author)
        if chosen is None:
            continue

        # Annotate quality with strategy tag
        tagged = f"{quality}_{label}"
        # Test whether THIS strategy beats current best (or the original row)
        compare_quality = best_row["match_quality"] if best_row else current_quality
        if is_better(quality, compare_quality):
            best_row = _build_row(row, chosen, tagged)
            best_label = label

    return best_row, best_label


def _build_row(row, chosen, tagged_quality):
    """Same shape as mine_one's success path, but with a tagged match_quality."""
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
        "match_quality"         : tagged_quality,
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


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not os.path.exists(OUT_CSV):
        sys.exit(f"ERROR: {OUT_CSV} not found. Run mine_ipni_bhl.py first.")

    print(f"Reading {os.path.basename(OUT_CSV)} …")
    with open(OUT_CSV, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    print(f"  {len(rows):,} rows total")

    targets = [i for i, r in enumerate(rows) if r["match_quality"] in RETRY_TARGETS]
    print(f"  {len(targets):,} retry candidates "
          f"({RETRY_TARGETS})")

    if not targets:
        print("Nothing to retry.")
        return

    log_fh = open(LOG_FILE, "w", encoding="utf-8")
    log_fh.write("strategy\told_quality\tnew_quality\ttaxonID\tscientificName\tipni_name\n")

    n_improved = n_autonym = n_gender = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(retry_one, rows[i]): i for i in targets}
        for j, future in enumerate(as_completed(futures), 1):
            i = futures[future]
            new_row, label = future.result()
            if new_row is not None:
                old_q = rows[i]["match_quality"]
                rows[i] = new_row
                n_improved += 1
                if label == "autonym": n_autonym += 1
                if label == "gender":  n_gender  += 1
                log_fh.write(f"{label}\t{old_q}\t{new_row['match_quality']}\t"
                             f"{new_row['taxonID']}\t{new_row['scientificName']}\t"
                             f"{new_row['ipni_name']}\n")
            elapsed = time.time() - t0
            rate = j / elapsed if elapsed else 0
            eta  = (len(targets) - j) / rate if rate else 0
            print(f"  [{j:>4}/{len(targets)}]  improved so far: {n_improved}  "
                  f"|  ETA {eta/60:.1f}min", end="\r", flush=True)

    log_fh.close()
    print(f"\n\n{'='*65}")
    print(f"  Retry candidates       : {len(targets):,}")
    print(f"  Improved by autonym    : {n_autonym:,}")
    print(f"  Improved by gender swap: {n_gender:,}")
    print(f"  TOTAL improved         : {n_improved:,}")
    print(f"  Log file               : {LOG_FILE}")

    print(f"\nWriting updated {os.path.basename(OUT_CSV)} …")
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=OUT_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(_clean_strings(r))


if __name__ == "__main__":
    main()
