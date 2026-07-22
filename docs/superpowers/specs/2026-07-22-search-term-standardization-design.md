# Search Term Standardization — Design

**Date:** 2026-07-22
**Status:** Approved for planning

## Problem

Every weekly run re-generates a search term for every product from scratch via LLM, even for
products that get searched the same way every week and reliably produce good matches. There is
no memory of which search terms have historically worked well, so the same "reinventing the
wheel" happens over and over, at ongoing LLM cost.

## Goal

Build a standing library of "proven good" search terms, tracked per product **per store** (the
same product may need a different term at each competitor site). Once a term has proven itself,
skip LLM generation for that product/store and use the proven term directly. Surface all changes
to the operator for approval — nothing is adopted or dropped silently.

## Key constraint discovered during design

Products are identified in the database by `offer_parts.id`, which is **not stable across
weeks** — a new row is created each week for what is conceptually the same product.
`offer_parts.code` is the stable identifier, but it isn't perfectly constant either: a
one-character suffix (historically Greek letters, but also alphanumeric/symbols) is appended
each week during offer rollover, and is sometimes absent the first week an item exists. The
existing PHP system determines "this week's suffix" via a majority vote of the last character
across all `code` values in an offer:

```php
$suffixGuesser = function (\App\Models\Offer $offer): string {
    $suffixCounts = [];
    foreach ($offer->offer_parts as $part) {
        $suffix = mb_substr($part->code, -1);
        if (!mb_strlen($suffix)) continue;
        $suffixCounts[$suffix] = ($suffixCounts[$suffix] ?? 0) + 1;
    }
    return array_search(max($suffixCounts), $suffixCounts);
};
```

Neither `offer_parts.id` nor raw `code` is currently written to any scraper output file
(CSV, JSON, or HTML report) — only `id` and the product name (`db_name`) are. So today's files
cannot be reliably joined product-to-product across weeks.

**Empirical validation (see Backfill section):** across the 24 weeks of existing output files,
exact `db_name` string matching links 96.6% of products across multiple weeks with zero
observed drift (no case where the same product's name changed between weeks). This makes
exact-name matching viable as a **one-time backfill** mechanism, even though `code` (suffix
normalized) is the correct **ongoing** mechanism.

## Data model

Three new files, all gitignored (contain business data, not code), at the project root:

### `search_term_history.json`
Append-only log written after every run. Maps a normalized **base code** → store → list of
observations:

```json
{
  "AAPL": {
    "coles": [
      {"term": "Pink Lady Apples", "quality": "good", "offer_id": 120, "date": "2026-05-20"},
      {"term": "Pink Lady Apples", "quality": "good", "offer_id": 121, "date": "2026-05-27"}
    ],
    "woolworths": [
      {"term": "Pink Lady Apples", "quality": "ok", "offer_id": 120, "date": "2026-05-20"}
    ]
  }
}
```

### `learned_search_terms.json`
The currently **active, approved** standard terms — one per (base code, store):

```json
{
  "AAPL": {
    "coles": {"term": "Pink Lady Apples", "good_count": 2, "approved_date": "2026-06-03"},
    "woolworths": {"term": "Fresh Pink Lady Apples", "good_count": 3, "approved_date": "2026-06-10"}
  }
}
```

### Precedence
`search_overrides.json` (manual, hand-typed) always wins over `learned_search_terms.json`
(auto-learned) for the same product/store. The manual file is untouched by this feature.

### Base-code normalization
For each run, compute the majority trailing-suffix character across all fetched items' `code`
values (port of the PHP `$suffixGuesser` logic to Python). Strip that character from the end of
each item's `code` (only if present) to get its `base_code`. This is the join key used
everywhere in this feature. If there's no clear majority (e.g. a single-item run), skip
stripping and use the raw code as-is — matching degrades gracefully rather than erroring.

## Pipeline changes

`code` must be threaded through the pipeline, which currently drops it after the DB-fetch step:
- `write_temp_csv` (pipeline.py) — add a `code` column
- `pipeline_input_dedup_*.csv` — preserve `code` through deduplication
- scraper input/output (`main.py`, `models.py: InputItem`) — carry `code` through to results JSON
- `pipeline_comparison_*.csv` (processor.py: `ComparisonRow`, `write_csv`) — add a `code` column

## Per-store search terms (scraper change)

Today, `main.py` searches both stores with the **same** `item.extracted_search_term`
(`scraper/main.py:81`, `scraper/stores/base.py:104`). This changes so each store can be given
its own term for the same item: before search-term generation, for each item/store pair, check
(in order) `search_overrides.json` → `learned_search_terms.json` → LLM-generated term. The two
stores may end up searching different terms for the same product. This does **not** increase
the number of site requests — one search per store still happens, same as today; only which
term is used per store changes.

## Weekly flow

1. Fetch items (as today), now including `code`. Compute this run's `base_code` per item.
2. Resolve search terms per item **per store**: manual override → learned term → fresh LLM guess.
3. Scrape both stores using the resolved (possibly different) per-store terms.
4. Process results as today.
5. Append this run's (base_code, store, term, quality, offer_id, date) to
   `search_term_history.json`.
6. Compute **promotion candidates**: any (base_code, store) whose term has reached
   `search_term_min_good_results` (default 2, need not be consecutive) "good" results, and isn't
   already the active learned term.
   - If more than one distinct term for the same (base_code, store) qualifies, only the
     strongest is proposed: most good results, most recent good result as tiebreaker. Only one
     term is ever active per (base_code, store) at a time.
7. Compute **decay flags**: any (base_code, store) with an active learned term that just
   returned `poor`/`none` this run. If a proven alternative term exists in the history, it's
   suggested as the replacement — never swapped automatically.
8. If there are any candidates or decay flags, show the review screen (skipped entirely in
   `--fully-automated` mode; pending candidates just persist for the next interactive run since
   the computation in steps 6–7 is idempotent over the history log).

## Review screen

New wizard step, modeled on the existing approval screen (`wizard/steps/approval.py`):
- **New promotions** — checkbox list, approving writes the term into `learned_search_terms.json`.
- **Failing terms** — checkbox list of previously-approved terms that just failed. If a proven
  alternative exists in the history, it's shown and checking the row swaps the active term to
  that alternative; if no alternative exists, checking the row simply removes the entry (falling
  back to a fresh LLM guess next run). Leaving a row unchecked keeps the current (failing) term
  active either way.
- Only appears when there's at least one candidate or flag that week.

## One-time backfill script

A standalone developer script (`scripts/backfill_search_term_history.py`, not part of the normal
pipeline):
1. Parse every `pipeline_input_dedup_*.csv` + matching `pipeline_comparison_*.csv` pair in
   `output/`, building a history keyed by exact `db_name` (as validated above).
2. Query the database for current items (`code`, `name`) to get a name → base_code mapping for
   currently active products.
3. For each historical name with a current-product match, write its history into
   `search_term_history.json` under that product's base_code. Names with no current match
   (e.g. discontinued products) are dropped, and the script logs a summary count of how many
   names were linked vs. dropped so nothing is silently lost.
4. Run once, manually, after the pipeline changes ship.

## Settings

Add `search_term_min_good_results: int = 2` to `Settings` (wizard/settings.py), editable the
same way as other numeric settings (Options screen → E to edit `settings.json`).

## Error handling

- `learned_search_terms.json` / `search_term_history.json` malformed or unreadable: same pattern
  as `search_overrides.py`'s `MalformedOverridesError` — interactive run prompts to continue
  without them (`[Y/n]`), non-interactive (`--fully-automated` or no TTY) aborts.
- Missing `code` on an item: already handled by the existing override-resolution warning
  ("no items have a 'code' value"); this feature reuses that same guard and simply skips
  learned-term lookups for that run.

## Testing

- Unit tests for base-code suffix normalization (port of `$suffixGuesser`), including: no
  suffix, mixed suffixes in one run, single-item run (no majority).
- Unit tests for promotion-candidate selection (threshold, multi-candidate tie-break).
- Unit tests for decay detection and replacement suggestion.
- Unit test for the backfill script against a small fixture subset of real output files,
  asserting linked/dropped counts.
- Manual end-to-end wizard run with a seeded `learned_search_terms.json` to confirm: the review
  screen appears only when there's something to review, approving a promotion persists it, and
  a promoted term is actually used (bypassing LLM generation) on the next run.

## Out of scope

- Rotating/multi-candidate fallback per (base_code, store) — only one active term per pair.
- Automatic adoption or automatic removal without operator approval.
- Recovering history for products no longer in the current database.
