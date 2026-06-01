# FotW Bibliography — IPNI mining for cross-database links + BHL protologue scans

For every taxon in the FotW taxonomy DB, mine [IPNI](https://www.ipni.org/) (International Plant Names Index) to enable four things on the FotW taxon page:

1. **An IPNI record** for the taxon — `ipni_id` + URL, canonical name, author, protologue citation. IPNI is the authoritative nomenclatural index for vascular plants (Kew + Harvard + Australian National Herbarium).
2. **A cross-link to [World Flora Online (WFO)](https://www.worldfloraonline.org/)** — IPNI records carry a WFO ID, which we turn into a direct WFO taxon-page URL.
3. **A cross-link to [Plants of the World Online (POWO)](https://powo.science.kew.org/)** — IPNI records flag whether POWO has a profile; when yes, we build the POWO URL.
4. **The protologue page on [BHL (Biodiversity Heritage Library)](https://www.biodiversitylibrary.org/)** — IPNI records embed an OpenURL to the scanned page where the species was first described. We extract that URL so users can click through and read the original description in BHL.

In addition, the deliverable carries the **GBIF accepted-species ID** for every taxon — copied from `fotw_taxonomy_resolved.csv` (the input), no extra API call. This gives a fifth cross-database identifier on the same row.

**Input:** `../FotW_DB/fotw_taxonomy_resolved.csv` (12,830 taxa, accepted + synonyms).
**API:** IPNI (`https://www.ipni.org/api/1/`, free, no key required). One call per taxon — WFO, POWO, BHL, and GBIF links are all derived from IPNI's response (BHL OpenURLs) or from the FotW source (GBIF). No separate BHL / WFO / POWO / GBIF calls.
**Output:** `fotw_ipni_bhl.csv` — one row per FotW taxon, 25 columns.

## Why mine IPNI rather than query BHL directly for the protologue

The intuitive approach would be to query BHL's `NameSearch` for each species name. We tried this first and abandoned it: `NameSearch` returns OCR-detected mentions of a name across the BHL corpus — typically hundreds of unranked mentions per name, with no way to single out the *protologue* (the publication where the name was originally described). BHL also throttles at modest concurrency.

IPNI sidesteps both problems. Each IPNI record is curated and carries:

- the canonical protologue citation (publication, volume, page, year)
- a stable IPNI ID and URL
- a WFO ID (cross-link to World Flora Online) for ~94% of records
- `inPowo` flag (cross-link to Plants of the World Online) for ~92% of records
- for ~54% of records, a direct **OpenURL link to the BHL scanned page** where the species was first described
- for ~78% of records, a link to the publication in BHL

So one IPNI call per taxon gives us a clean protologue link in BHL **and** the cross-database identifiers we need for the FotW UI.

## Pipeline

```
fotw_taxonomy_resolved.csv
            │
            ▼
[Step 1] mine_ipni_bhl.py
   For each FotW taxon:
     - Strip the author from scientificName → canonical "Genus + epithet"
     - Query IPNI /api/1/search
     - Pick best record by exact name + author
     - Parse IPNI id, protologue ref, BHL/WFO/POWO links
            │
            ▼
   fotw_ipni_bhl.csv  (first pass — ~97.3% confident matches)
            │
            ▼
[Step 2] retry_low_quality.py   (optional but recommended)
   For each no_match / fuzzy_* row:
     - Try stripping redundant infraspecific autonyms
       e.g. "Croton gratissimus var. gratissimus" → "Croton gratissimus"
     - Try gender suffix variants
       e.g. "Aleurites moluccana" → "...moluccanus" / "...moluccanum"
     - Replace the row in place if a higher-quality match is found
            │
            ▼
   fotw_ipni_bhl.csv  (final — 98.11% confident matches)
```

### Step 1 — `mine_ipni_bhl.py`

For each FotW taxon:

1. Strip the author from `scientificName` to get the canonical `Genus + epithet (+ infraspecific)`.
2. Query IPNI's search API with the canonical form.
3. From the up-to-20 returned hits, choose the best by:
   - exact canonical name match + exact author match (highest priority), or
   - exact canonical name + `topCopy = true`, or
   - exact canonical name (any), or
   - first hit (fallback).
4. Parse the chosen record for IPNI id, protologue reference, BHL/WFO/POWO links, type note, and distribution.
5. HTML-unescape every string field before writing.

Resumable — already-mined `taxonID` values are skipped on re-run.

### Step 2 — `retry_low_quality.py`

After the first pass, two cheap retry strategies recover ~30% of the remaining low-quality rows:

| Strategy | Description | Example |
|---|---|---|
| **Autonym stripping** | Drop the redundant infraspecific autonym (IPNI doesn't store autonyms as separate records). | `Croton gratissimus var. gratissimus` → `Croton gratissimus` (Burch.) |
| **Gender swap** | Try alternate Latin gender endings (`-a` / `-us` / `-um`) for binomial names. | `Aleurites moluccana` → `Aleurites moluccanus` (L.) Willd. |

Only replaces a row when the new match is genuinely higher quality. Recovered rows carry a tagged `match_quality` (e.g., `exact_top_copy_autonym`, `exact_with_author_gender`) so the source of the fix stays auditable.

#### Recovery summary (on the actual 12,830-taxon run)

| | Before retry | After retry |
|---|---:|---:|
| Confident matches (`exact_*`) | 12,484 (97.30%) | **12,587 (98.11%)** |
| `no_match` rows | 214 | 192 |
| `fuzzy_top_copy` / `fuzzy_canonical` rows | 132 / 4 | 47 / 4 |
| **Retry candidates considered** | — | **346** |
| Recovered by autonym stripping | — | **79** |
| Recovered by gender swap | — | **24** |
| **Total recovered** | — | **103** |

The autonym fix is the bigger lever (79 recoveries) because FotW often carries `Genus species RANK species` autonyms verbatim while IPNI only stores the base species. The gender swap (24 recoveries) catches name changes between the original Latin gender (e.g. *moluccana*) and the currently accepted ending (*moluccanus*). Every recovered row is logged to `retry_low_quality.log` (TSV: strategy · old_quality · new_quality · taxonID · scientificName · ipni_name).

## Usage

```bash
# Step 1: mine all 12,830 FotW taxa (~12 min wall clock at 6 workers × 0.3s)
python3 mine_ipni_bhl.py
python3 mine_ipni_bhl.py --limit 30           # pilot
python3 mine_ipni_bhl.py --workers 6 --delay 0.3

# Step 2: retry low-quality rows in place (~2 min)
python3 retry_low_quality.py
```

## Output schema (`fotw_ipni_bhl.csv`)

| Column | Description |
|---|---|
| `taxonID` | FotW taxon UUID |
| `scientificName` | FotW name as queried |
| `accepted_gbif_id` | GBIF accepted species key (from `fotw_taxonomy_resolved.csv`). Same join key used by the EDGE releases. |
| `accepted_gbif_url` | `https://gbif.org/species/{accepted_gbif_id}` |
| `ipni_id` | IPNI name ID (e.g., `320700-2`) |
| `ipni_url` | `https://www.ipni.org/n/{ipni_id}` |
| `ipni_name` | Canonical name from IPNI |
| `ipni_author` | Author citation from IPNI |
| `match_quality` | See match-quality scale below |
| `protologue_reference` | Full citation, e.g. `Sp. Pl. 2: 951. 1753` |
| `protologue_year` | Year of original publication |
| `protologue_publication` | Publication title or abbreviation |
| `protologue_collation` | Volume:page (e.g., `2: 951`) |
| `bhl_page_id` | BHL page ID for the protologue (when present) |
| `bhl_page_url` | `https://www.biodiversitylibrary.org/page/{bhl_page_id}` |
| `bhl_title_id` | BHL bibliography ID for the publication |
| `bhl_title_url` | `https://www.biodiversitylibrary.org/bibliography/{bhl_title_id}` |
| `type_note` | Typification reference (when given; ~1% of records) |
| `distribution` | Free-text geographic note from IPNI |
| `wfo_id` | World Flora Online ID |
| `wfo_url` | `https://www.worldfloraonline.org/taxon/{wfo_id}` |
| `in_powo` | `y` / `n` — whether POWO has a profile |
| `powo_url` | `https://powo.science.kew.org/taxon/urn:lsid:ipni.org:names:{ipni_id}` |
| `n_same_citation_as` | Number of alternative IPNI records pointing at the same citation (duplicate-detection signal) |
| `error` | Error message if the IPNI request failed |

### Match quality scale (best → worst)

| Value | Meaning |
|---|---|
| `exact_with_author` | Canonical name AND author both match the FotW input — strongest confidence. |
| `exact_with_author_autonym` | Recovered after stripping a redundant autonym; canonical name + author match. |
| `exact_with_author_gender` | Recovered after gender-suffix swap; canonical name + author match. |
| `exact_top_copy` | Canonical name matches; IPNI flagged the record as `topCopy` (canonical entry). Author not confirmed (often because FotW didn't carry one). |
| `exact_top_copy_autonym` | Same as above, recovered via autonym stripping. |
| `exact_top_copy_gender` | Same as above, recovered via gender swap. |
| `exact_canonical` | Canonical name matches, no `topCopy` flag — multiple records of equal status. |
| `fuzzy_top_copy` | No exact canonical match; first `topCopy` from the search results. |
| `fuzzy_canonical` | No exact canonical match; first available result. |
| `no_match` | IPNI returned no results for the canonical query and all retries. |

## Final coverage (12,830 FotW taxa)

| Metric | Count | % |
|---|---:|---:|
| Confident matches (all `exact_*`) | **12,587** | **98.11%** |
| GBIF accepted ID + URL | 12,827 | 99.97% |
| IPNI ID + URL | 12,587 | 98.1% |
| WFO ID + URL | 12,129 | 94.5% |
| POWO URL (`in_powo = y`) | 11,788 | 91.9% |
| BHL title URL | 9,986 | 77.8% |
| BHL protologue page URL | 6,913 | 53.9% |
| Distribution text | 2,111 | 16.5% |
| Type note | 96 | 0.7% |
| Errors | 0 | 0.0% |

Match-quality breakdown:

| Quality | Count |
|---|---:|
| `exact_with_author` | 7,278 |
| `exact_top_copy` | 5,206 |
| `exact_top_copy_autonym` | 79 |
| `exact_top_copy_gender` | 14 |
| `exact_with_author_gender` | 10 |
| `fuzzy_top_copy` | 47 |
| `fuzzy_canonical` | 4 |
| `no_match` | 192 |

The 192 remaining `no_match` rows (1.5%) are split between genuine IPNI gaps (rare older names, obscure synonyms) and family-rank or bare-genus FotW entries that IPNI doesn't index at the right granularity. Diminishing returns to chase further.

Protologue year span: **1753–2024** (median 1858). Most FotW taxa were named in the 19th century.

### Example output — *Cypripedium calceolus* L.

```
taxonID                : e9b6e6a7-04b1-3f6f-b7bf-9363599a8981
scientificName         : Cypripedium calceolus L.
accepted_gbif_id       : 2820517
accepted_gbif_url      : https://gbif.org/species/2820517
ipni_id                : 320700-2
ipni_url               : https://www.ipni.org/n/320700-2
ipni_name              : Cypripedium calceolus
ipni_author            : L.
match_quality          : exact_with_author
protologue_reference   : Sp. Pl. 2: 951. 1753 [1 May 1753]
protologue_year        : 1753
protologue_publication : Species Plantarum
protologue_collation   : 2: 951
bhl_page_url           : https://www.biodiversitylibrary.org/page/33355180
bhl_title_url          : https://www.biodiversitylibrary.org/bibliography/669
type_note              : Dodoens, Stirp. Hist. Permpt. ed. 2, 180, fig. 1, 2. 1616
distribution           : Europe, Asia & North America
wfo_id                 : wfo-0000935209
wfo_url                : https://www.worldfloraonline.org/taxon/wfo-0000935209
in_powo                : y
powo_url               : https://powo.science.kew.org/taxon/urn:lsid:ipni.org:names:320700-2
```

## FotW display recommendation

Three tiers of cross-database identifiers + one bibliographic link, render whichever data is present:

| Field present | Suggested label / link | Notes |
|---|---|---|
| `accepted_gbif_url` | "GBIF" badge → `accepted_gbif_url` | Cross-link to the Global Biodiversity Information Facility. Available for ~100% of taxa. Same key used by the EDGE releases. |
| `ipni_url` | "IPNI: {ipni_id}" badge → `ipni_url` | Nomenclatural provenance. Available for 98% of taxa. |
| `wfo_url` | "WFO" badge → `wfo_url` | Cross-link to World Flora Online. Available for 94%. |
| `powo_url` | "POWO" badge → `powo_url` | Cross-link to Plants of the World Online (Kew). Available for 92%. |
| `bhl_page_url` | "View original description on BHL" → `bhl_page_url` | Direct link to the scanned page of the protologue. The single most useful link for users (54% coverage). |
| `bhl_title_url` | "View publication on BHL" → `bhl_title_url` | Fallback when `bhl_page_url` is empty; links to the whole publication (78% coverage). |
| `protologue_reference` | inline citation under the species name | E.g., *"Originally described by Linnaeus in Sp. Pl. 2: 951. 1753."* |

For taxa where IPNI has no record (~2%), show none of the above — let FotW fall back to its existing metadata.

## File inventory

| File | Purpose |
|---|---|
| `mine_ipni_bhl.py` | Step 1 — main miner. Resumable. |
| `retry_low_quality.py` | Step 2 — autonym + gender-swap retry. |
| `fotw_ipni_bhl.csv` | **Deliverable.** 12,830 rows × 25 columns. |
| `mine_ipni_bhl.log` | Per-row error log from Step 1. |
| `retry_low_quality.log` | TSV log of every row updated by Step 2 (old quality → new quality → IPNI match). |
| `README.md` | This file. |

## Citation

- The International Plant Names Index. (2026). Published on the Internet <https://www.ipni.org>, The Royal Botanic Gardens, Kew, Harvard University Herbaria & Libraries and Australian National Botanic Gardens.
- World Flora Online. (2026). <http://www.worldfloraonline.org>.
- Plants of the World Online. (2026). Royal Botanic Gardens, Kew. <https://powo.science.kew.org>.
- Biodiversity Heritage Library. (2026). <https://www.biodiversitylibrary.org>.

## License

Code: CC BY 4.0 (consistent with the other FotW-supporting repos in this project). IPNI, WFO, POWO, and BHL data are governed by their respective terms of use.
