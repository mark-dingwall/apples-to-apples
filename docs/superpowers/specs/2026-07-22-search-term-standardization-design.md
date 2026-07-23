# Search Term Standardization — Design

**Date:** 2026-07-22
**Status:** Revised after multi-reviewer design review (round 1)

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

**Caveat on that validation (added in review):** the 96.6%/zero-drift figures were measured
*among the 24 historical weeks themselves*. The backfill's actual name→product link (see
Backfill section) is made against whatever the database looks like at the moment the backfill
script is run — a further, unmeasured gap beyond the last historical week, during which a name
could in principle have been reused for a different product. The name-shadow safeguard below
exists specifically to catch that case rather than trusting the historical statistic to cover it.

## Data model

Three new files, all gitignored (contain business data, not code), at the project root:

### `search_term_history.json`
Append-only log written after every run (see exclusions below). Maps a normalized **base
code** → store → list of observations. Each observation carries the product name at the time it
was recorded — not used for matching, only as a safety check (see "Name-shadow safeguard"):

```json
{
  "AAPL": {
    "coles": [
      {"term": "Pink Lady Apples", "quality": "good", "offer_id": 120, "date": "2026-05-20", "name": "Apples - Pink Lady - approx. 157g"},
      {"term": "Pink Lady Apples", "quality": "good", "offer_id": 121, "date": "2026-05-27", "name": "Apples - Pink Lady - approx. 157g", "source": "backfill"}
    ],
    "woolworths": [
      {"term": "Pink Lady Apples", "quality": "ok", "offer_id": 120, "date": "2026-05-20", "name": "Apples - Pink Lady - approx. 157g"}
    ]
  }
}
```

`source: "backfill"` marks entries written by the one-time backfill script (see below); entries
from normal weekly runs omit this field. This is what makes the backfill safe to re-run.

### `learned_search_terms.json`
The currently **active, approved** standard terms — one per (base code, store). Also carries the
product name at approval time, for the same safety-check purpose:

```json
{
  "AAPL": {
    "coles": {"term": "Pink Lady Apples", "good_count": 2, "approved_date": "2026-06-03", "name": "Apples - Pink Lady - approx. 157g"},
    "woolworths": {"term": "Fresh Pink Lady Apples", "good_count": 3, "approved_date": "2026-06-10", "name": "Apples - Pink Lady - approx. 157g"}
  }
}
```

### `dismissed_search_terms.json`
Records promotion candidates the operator explicitly declined, so they don't resurface every
week. Keyed the same way, one entry per declined (base_code, store, term):

```json
{ "AAPL": { "coles": ["Pink Lady 157g apples"] } }
```

A candidate whose (base_code, store, term) tuple appears here is excluded from future "New
promotions" lists, even though it still technically meets the good-result threshold. Declining a
row on the review screen writes it here.

### Precedence
`search_overrides.json` (manual, hand-typed) always wins over `learned_search_terms.json`
(auto-learned) for the same product/store. The manual file is untouched by this feature.

### Base-code normalization
For each run, compute the majority trailing-suffix character across all fetched items' `code`
values (port of the PHP `$suffixGuesser` logic to Python), with two precision fixes the PHP
version doesn't need (it only has to guess a display suffix; we're using the result as a data
join key, so silent wrong guesses are more costly for us):

- **Majority is strict.** A character only counts as "this week's suffix" if it's a strict
  majority (>50% of items with a non-empty last character), not merely the most common one. If
  there's no strict majority (including an exact tie between two characters), treat the run as
  having no detectable suffix this week and use every item's raw `code` unstripped — do not port
  PHP's `array_search(max(...))`, which silently resolves ties to an arbitrary key and would
  strip the wrong character for whichever items don't happen to use it.
- **Empty-result guard.** If stripping the suffix character would leave an empty string, don't
  strip it for that item (skip stripping only for that one item, not the whole run).

**Known, accepted limitation:** this is still a heuristic, not a guarantee. An item's *native*
last character can coincidentally match this week's guessed suffix (most likely in an item's
first, not-yet-suffixed week), causing that one item's `base_code` to differ from its own
`base_code` in other weeks. The effect is silent fragmentation of *that item's own* history
(it just accumulates slower / never reaches the promotion threshold), not corruption of another
product's data — the name-shadow safeguard below exists to catch the more serious failure mode
(two *different* products colliding onto the same `base_code`). Fully eliminating per-item
fragmentation would require information the system doesn't have (whether a given trailing
character is a real part of the code or an appended suffix), so it's accepted as a known
limitation rather than solved.

### Name-shadow safeguard (new — closes a collision risk raised in review)
Because `base_code` is a heuristic, two different products could in principle end up sharing one
(rare, but the override-matching code already treats the analogous "one code prefix matches
multiple items" case as a real, expected occurrence — see `scraper/utils/overrides.py:169-179`).
Before writing a new history observation or promoting/using a learned term for a `(base_code,
store)`, compare the item's current `name` against the `name` already on file for that
`(base_code, store)` (from the most recent history entry, or the active learned term):
- **Match (or nothing on file yet):** proceed normally.
- **Mismatch:** log a warning ("base_code {X} previously seen as {old_name}, now {new_name} —
  skipping history/learned-term use for this item this run") and treat this item as if it had no
  history for that `(base_code, store)` this run — i.e. don't append to its history, don't use
  its learned term, don't count it toward any candidate. This turns a silent, invisible merge
  into a visible, logged skip. It does not attempt to auto-resolve which product is "right."

## Pipeline changes

`code` must be threaded through the pipeline, which currently drops it after the DB-fetch step.
There are **two** parallel entry points that build the item CSV today and both need the change
(they share the same schema but are separate functions):
- `write_temp_csv` (`scraper/pipeline.py:135`) — the CLI-automation path.
- `write_pipeline_csv` (`scraper/wizard/steps/progress.py:133`) — the interactive-wizard path
  (the one CLAUDE.md's Quick Start recommends; easy to miss since it's a near-duplicate of the
  above under a different name).

Both need a `code` column added. `db_name` (already present) also becomes load-bearing for the
name-shadow safeguard, not just display.

## Per-store search terms (scraper change)

Today, `main.py` searches both stores with the **same** `item.extracted_search_term`
(`scraper/main.py:81`, `scraper/stores/base.py:104`) — `search()` itself is store-agnostic and
needs no change; the actual constraint is that `InputItem` (`scraper/models.py:31`) carries only
one term. Review surfaced that this is a materially bigger change than "each store looks up its
own term" implies, because of an existing performance optimization built entirely around one
term per item: `deduplicate_for_scraping()` (`scraper/wizard/steps/progress.py:173-194`) groups
items by shared `search_term` and scrapes each unique term only once, then `_expand_deduplicated_
results()` (`progress.py:197-251`) copies that one result back out to every item that shared the
term. Both stores are scraped from the *same* deduplicated list in a single scraper invocation
today (`scraper/main.py:196-232`).

With per-store terms, the same item can need a different term at each store, so "shared term"
is no longer a property of an item — it's a property of an (item, store) pair. Concretely, this
requires:
- `InputItem.extracted_search_term` (`models.py:31`) becomes two fields (e.g.
  `search_term_coles` / `search_term_woolworths`) or a `dict[str, str]` keyed by store.
- Both CSV writers above (`write_temp_csv`, `write_pipeline_csv`) gain **two** search-term
  columns instead of one, and `load_items`/`load_input_items` (`main.py`, `processor.py`) read
  both.
- `deduplicate_for_scraping` and `_expand_deduplicated_results` are reworked to dedupe/expand
  keyed on `(term, store)` rather than `term` alone — the dedup savings still apply, just scoped
  per store instead of globally.
- `ProcessorInputItem`/`ComparisonRow` (`processor.py:52-59, 84`) gain a per-store search-term
  field (needed anyway, since the history-append step needs to know exactly which term was
  searched at which store) instead of the current single `search_term`.
- `scraper/oos_mode.py` is single-store already, so it only needs to look up the one relevant
  store's term — minimal impact.

This still does **not** increase the number of site requests: `scrape_store()`
(`scraper/main.py:69-92`) calls `search()` exactly once per item per store regardless of which
term string is supplied, and `run_scraper()` invokes the store-A and store-B scrape passes
exactly once each per run (`main.py:196-232`) — changing *which* term is passed doesn't add
calls, it only changes the dedup grouping, per the previous bullet.

## Weekly flow

1. Fetch items (as today), now including `code`. Compute this run's `base_code` per item
   (majority-vote suffix strip, with the strict-majority and empty-guard rules above).
2. Resolve search terms per item **per store**: manual override → learned term (subject to the
   name-shadow check) → fresh LLM guess.
3. Scrape both stores using the resolved (possibly different) per-store terms.
4. Process results as today. **History logging (step 5) uses the processor's raw, LLM-assigned
   `match_quality` for each store** — not the operator's later approve/reject decision on the
   Approval screen. These are different signals: match quality answers "did the search find the
   right product," approval answers "should this price update be accepted" (which can be
   rejected for reasons unrelated to match correctness, e.g. a price outlier). This step runs
   right after Progress, independent of and before the Approval step.
5. Append this run's `(base_code, store, term, quality, offer_id, date, name)` to
   `search_term_history.json` — **except** when the run was invoked with `--dry-run` or
   `--limit`. Both are explicitly non-representative preview/test runs (per CLAUDE.md), and
   writing their observations into the production ledger would let a manual sanity-check
   silently influence real promotion decisions.
   - For each store, only log an observation (and only let it feed decay/promotion detection
     below) if that store's scrape actually completed (`StoreResults.status == "success"`).
     A `blocked`/`error`/`no_results` run produces `match_quality == "none"` for reasons that
     have nothing to do with whether the search term itself is any good (see CLAUDE.md's
     "Known Issues": stores occasionally block after ~30 items) — counting that as a real
     "poor" observation would flag good terms as decayed over a transient site block.
6. Compute **promotion candidates**: any (base_code, store) whose term has reached
   `search_term_min_good_results` (default 2, need not be consecutive) "good" results, isn't
   already the active learned term, and isn't present in `dismissed_search_terms.json` for that
   pair.
   - If more than one distinct term for the same (base_code, store) qualifies, only the
     strongest is proposed: most good results, most recent good-result date as first tiebreak,
     and the term string sorted alphabetically as a final, fully deterministic tiebreak (so
     selection is never dependent on incidental iteration/insertion order). Only one term is
     ever active per (base_code, store) at a time.
   - **A (base_code, store) pair is only ever a "New promotion" candidate if it currently has
     no active learned term.** If it already has an active term, any other qualifying term for
     that pair is *only* surfaced as the suggested replacement on a decay flag (step 7), never
     also as an independent "New promotion" row — see the Review screen section for why this
     matters.
7. Compute **decay flags**: any (base_code, store) with an active learned term whose most recent
   *counted* observation (per the status filter in step 5) was `poor`/`none`. If a proven
   alternative term exists in the history (the same candidate logic as step 6, minus the
   "no active term" restriction), it's suggested as the replacement — never swapped
   automatically.
8. If there are any candidates or decay flags, show the review screen (skipped entirely in
   `--fully-automated` mode; pending candidates just persist for the next interactive run since
   the computation in steps 6–7 is idempotent over the history log).

## Review screen

New wizard step, modeled on the existing approval screen (`wizard/steps/approval.py`), shown
after Progress:
- **New promotions** — checkbox list of (base_code, store) pairs with no active term yet.
  Approving writes the term into `learned_search_terms.json`; declining writes it to
  `dismissed_search_terms.json` so it isn't proposed again.
- **Failing terms** — checkbox list of previously-approved terms whose latest counted result was
  poor/none. If a proven alternative exists, it's shown and checking the row swaps the active
  term to that alternative; if no alternative exists, checking the row simply removes the entry
  (falling back to a fresh LLM guess next run). Leaving a row unchecked keeps the current
  (failing) term active either way. Because step 6 excludes any term from "New promotions" once
  its pair has an active term, a proven alternative can only ever appear here — never
  simultaneously as its own separately-checkable "New promotion" row — so there's no scenario
  where approving one list silently contradicts a choice made on the other.
- Only appears when there's at least one candidate or flag that week.

## One-time backfill script

A standalone developer script (`scripts/backfill_search_term_history.py`, not part of the normal
pipeline), safe to re-run (see `source: "backfill"` marker above):
1. Parse every `pipeline_input_{offer}_{timestamp}.csv` (the **full**, non-deduplicated input
   CSV — not `pipeline_input_dedup_*.csv`, which only contains one row per unique search term
   and would silently skip every item that happened to share a term with another item) +
   matching `pipeline_comparison_*.csv` pair in `output/`, building a history keyed by exact
   `db_name` (as validated above).
2. Query the database for current items (`code`, `name`), and compute each one's `base_code`
   using the *exact same* suffix-normalization function used by the live pipeline (step 1 of the
   Weekly flow) — not a separate/ad-hoc implementation, to guarantee the backfilled keys and the
   live pipeline's future keys are computed identically.
3. Build a name → base_code mapping from that current-items list. If a name maps to **more than
   one** current product, log a warning and skip it (don't guess) — the same policy as the
   no-match case in step 4, just a different reason for being unlinkable.
4. For each historical name with exactly one current-product match, write its history into
   `search_term_history.json` under that product's base_code, tagged `"source": "backfill"`.
   Names with no current match (e.g. discontinued products) or an ambiguous match are dropped,
   and the script logs a summary count of how many names were linked, dropped-no-match, and
   dropped-ambiguous, so nothing is silently lost.
   - Before writing, apply the same name-shadow check as the live pipeline: if
     `search_term_history.json` already has a *different* name on file for that base_code/store
     (e.g. from a prior backfill run against a since-changed DB), skip and warn rather than
     overwrite.
5. Run once, manually, after the pipeline changes ship. Safe to re-run because entries are
   tagged with `source: "backfill"` — a re-run can replace only its own previously-written
   entries for a given base_code/store rather than appending duplicates that would inflate
   `good_count`.

## Settings

Add `search_term_min_good_results: int = 2` to `Settings` (`wizard/settings.py`), editable the
same way as other numeric settings (Options screen → E to edit `settings.json`). Note this
requires adding the field to **both** the `Settings` dataclass **and** `SettingsManager.
_from_dict` (`wizard/settings.py:108-158`), which manually enumerates every field by name when
loading from JSON — adding only the dataclass field would mean a value set in `settings.json`
is silently ignored and the default always wins.

## Error handling

- `learned_search_terms.json` / `search_term_history.json` / `dismissed_search_terms.json`
  malformed or unreadable: same pattern as `search_overrides.py`'s `MalformedOverridesError` —
  interactive run prompts to continue without them (`[Y/n]`), non-interactive
  (`--fully-automated` or no TTY) aborts.
- Missing `code` on an item: already handled by the existing override-resolution warning
  ("no items have a 'code' value"); this feature reuses that same guard and simply skips
  learned-term lookups for that run.
- Name-shadow mismatch: logged warning, item skipped for history/learned-term purposes this run
  (see "Name-shadow safeguard" above) — never a hard failure.

## Testing

- Unit tests for base-code suffix normalization: no suffix, mixed suffixes with a strict
  majority, an exact tie (must NOT strip), a single-item run (no majority), and an item whose
  stripped result would be empty (must not strip).
- Unit test for the name-shadow safeguard: same base_code, differing name → skip + warn, not a
  silent merge.
- Unit tests for promotion-candidate selection: threshold, multi-candidate tie-break including
  the full three-level tiebreak (good_count → most-recent-date → alphabetical), and the rule
  that a pair with an active term never produces a standalone "New promotion" row.
- Unit tests for decay detection: confirm a `blocked`/`error`/`no_results` status does NOT count
  as a decay signal, only a completed scrape with poor/none quality does.
- Unit test asserting override → learned-term → LLM precedence order end-to-end.
- Unit test for the backfill script against a small fixture subset of real output files,
  asserting linked/dropped-no-match/dropped-ambiguous counts, and that running it twice is a
  no-op on the resulting history (idempotency).
- Manual end-to-end wizard run with a seeded `learned_search_terms.json` to confirm: the review
  screen appears only when there's something to review, approving a promotion persists it, a
  promoted term is actually used (bypassing LLM generation) on the next run, and `--dry-run` /
  `--limit` runs do not write to `search_term_history.json`.

## Out of scope

- Rotating/multi-candidate fallback per (base_code, store) — only one active term per pair.
- Automatic adoption or automatic removal without operator approval.
- Recovering history for products no longer in the current database.
- Fully eliminating base_code fragmentation from the suffix-stripping heuristic (accepted
  limitation, see "Base-code normalization").
