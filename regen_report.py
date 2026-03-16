"""
Regenerate the HTML report for a given offer from existing on-disk artefacts.

Usage:
    python regen_report.py [offer_id]

If offer_id is omitted an error is shown.

What's reconstructed vs a full wizard run
------------------------------------------
  Fully reconstructed from disk:
    - All price comparison data          (pipeline_comparison CSV)
    - Old RRP per item                   (inferred from previous same-offer CSV)
    - Search terms                       (pipeline_input CSV)
    - Historical trend charts            (all past comparison CSVs)

  Approximated / optional:
    - SWOT analysis: the LLM-generated SWOT is not persisted to disk.
      This script offers two modes:
        1. Rule-based  -- instant, deterministic (default)
        2. LLM-based   -- calls Claude (same as full wizard run, ~30s)
"""

import csv
import re as _re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
if len(sys.argv) < 2:
    sys.exit("Usage: python regen_report.py <offer_id>")
OFFER_ID = int(sys.argv[1])
OUTPUT_DIR = Path("output")

# ---------------------------------------------------------------------------
# Locate comparison CSV (most recent for this offer)
# ---------------------------------------------------------------------------
def _csv_date(p: Path) -> str:
    m = _re.search(r'(\d{4}-\d{2}-\d{2}_\d{6})', p.name)
    return m.group(1) if m else ''

csv_candidates = sorted(OUTPUT_DIR.glob(f"pipeline_comparison_{OFFER_ID}_*.csv"), key=_csv_date)
if not csv_candidates:
    sys.exit(f"No pipeline_comparison_{OFFER_ID}_*.csv found in {OUTPUT_DIR}/")

csv_path = csv_candidates[-1]
print(f"Comparison CSV : {csv_path.name}")

# Extract timestamp from filename: pipeline_comparison_<id>_2026-01-15_103000.csv
parts = csv_path.stem.split("_")
timestamp = f"{parts[-2]}_{parts[-1]}"

# ---------------------------------------------------------------------------
# Build updates (same logic as pipeline --fully-automated)
# ---------------------------------------------------------------------------
from scraper.pipeline import parse_comparison_csv, build_updates

comparisons = parse_comparison_csv(csv_path)
updates = build_updates(comparisons)
print(f"Items          : {len(updates)} total")

# Try to read actual user-selected items from the audit log
audit_log_path = OUTPUT_DIR / f"pipeline_audit_{timestamp}.log"
if audit_log_path.exists():
    approved_ids: set[int] = set()
    with open(audit_log_path, encoding="utf-8") as _f:
        for _line in _f:
            if _line.startswith("ID: "):
                approved_ids.add(int(_line.strip()[4:]))
    for u in updates:
        u.selected = u.id in approved_ids
    approved = [u for u in updates if u.selected]
    print(f"Approved items : {len(approved)} (from audit log)")
else:
    approved = [u for u in updates if u.selected]
    print(f"Approved items : {len(approved)} (quality-based fallback, no audit log)")

# ---------------------------------------------------------------------------
# Infer old_rrp from previous same-offer comparison CSV
# ---------------------------------------------------------------------------
from scraper.stores.store_config import STORE_A_COL, STORE_B_COL
from scraper.html_report import _safe_int

all_comparison_csvs = sorted(OUTPUT_DIR.glob("pipeline_comparison_*.csv"), key=_csv_date)
current_date = _csv_date(csv_path)
prev_csv = next((p for p in reversed(all_comparison_csvs) if _csv_date(p) < current_date), None)
if prev_csv:
    print(f"Previous CSV   : {prev_csv.name}  (used for old RRP)")
    prev_rrps: dict[int, int] = {}
    try:
        with open(prev_csv, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                item_id = row.get("id")
                if not item_id:
                    continue
                sa = _safe_int(row.get(f"{STORE_A_COL}_converted_cents"))
                sb = _safe_int(row.get(f"{STORE_B_COL}_converted_cents"))
                prices = [p for p in [sa, sb] if p is not None]
                if prices:
                    prev_rrps[int(item_id)] = max(prices)
    except Exception as e:
        print(f"Warning: could not parse previous CSV for old RRP: {e}")

    for u in updates:
        if u.id in prev_rrps:
            u.old_rrp = prev_rrps[u.id]

    matched = sum(1 for u in updates if u.old_rrp is not None)
    print(f"Old RRP        : resolved for {matched}/{len(updates)} items")
else:
    print("Old RRP        : no previous CSV found -- RRP movement sections will be empty")

# ---------------------------------------------------------------------------
# Compute intermediates needed for SWOT
# ---------------------------------------------------------------------------
from scraper.wizard.steps.report import (
    _compute_price_positioning,
    _compute_store_coverage,
    _compute_cheapest,
    _compute_rrp_movement,
    _compute_outliers,
    _build_concerns,
    _load_csv_data,
)

csv_rows = _load_csv_data(csv_path)
positioning = _compute_price_positioning(updates)
coverage = _compute_store_coverage(csv_rows) if csv_rows else {
    "total": 0, "store_a_matches": 0, "store_a_good": 0, "store_a_ok": 0,
    "store_a_poor": 0, "store_b_matches": 0, "store_b_good": 0, "store_b_ok": 0,
    "store_b_poor": 0, "both": 0, "neither": 0, "single": 0,
}
cheapest = _compute_cheapest(updates)
rrp_mov = _compute_rrp_movement(updates)
outliers = _compute_outliers(updates)
concerns = _build_concerns(positioning, coverage, cheapest, rrp_mov, csv_rows)

# ---------------------------------------------------------------------------
# SWOT mode selection
# ---------------------------------------------------------------------------
from scraper.utils.swot import build_swot, build_swot_llm

print()
print("SWOT mode:")
print("  1. Rule-based  (instant)")
print("  2. LLM-based   (calls Claude, ~30s, same as full wizard run)")
choice = input("Choose [1/2, default=1]: ").strip()

if choice == "2":
    print("Generating LLM SWOT...")
    swot_data = build_swot_llm(positioning, coverage, cheapest, rrp_mov, concerns, outliers)
    if swot_data is None:
        print("LLM SWOT failed -- falling back to rule-based.")
        swot_data = build_swot(positioning, coverage, cheapest, rrp_mov)
    else:
        print("LLM SWOT complete.")
else:
    swot_data = build_swot(positioning, coverage, cheapest, rrp_mov)
    print("Rule-based SWOT generated.")

# ---------------------------------------------------------------------------
# Settings threshold
# ---------------------------------------------------------------------------
try:
    from scraper.wizard.settings import Settings
    threshold = Settings.load().significant_price_diff_pct
except Exception:
    threshold = 25.0

# ---------------------------------------------------------------------------
# Generate report
# ---------------------------------------------------------------------------
from scraper.html_report import generate_html_report

print()
html_path = generate_html_report(
    updates=updates,
    approved=approved,
    comparison_csv_path=csv_path,
    offer_id=OFFER_ID,
    timestamp=timestamp,
    output_dir=OUTPUT_DIR,
    settings_threshold=threshold,
    swot_data=swot_data,
)

if html_path:
    print(f"Report written : {html_path}")
else:
    print("Report generation failed.")
