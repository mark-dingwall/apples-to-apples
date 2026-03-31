"""
HTML report generator for price comparison pipeline.

Produces a self-contained HTML file with Chart.js charts, sortable tables,
and historical price trend analysis.
"""

import csv
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from statistics import stdev
from string import Template

from scraper.stores.store_config import STORE_A_NAME, STORE_B_NAME, STORE_A_COL, STORE_B_COL

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Historical data loading
# ---------------------------------------------------------------------------

def _load_historical_data(
    output_dir: Path, current_csv_path: Path | None,
) -> list[dict]:
    """
    Scan output/ for pipeline_comparison_*.csv and matching results JSON.
    Returns chronologically sorted list of run snapshots (max 10).
    """
    pattern = re.compile(
        r"pipeline_comparison_(\d+)_(\d{4}-\d{2}-\d{2})_(\d{6})\.csv$"
    )
    runs = []

    for csv_file in sorted(output_dir.glob("pipeline_comparison_*.csv")):
        # Skip the current run's CSV -- it will be handled separately
        if current_csv_path and csv_file.resolve() == current_csv_path.resolve():
            continue

        m = pattern.search(csv_file.name)
        if not m:
            continue

        offer_id = int(m.group(1))
        date_str = m.group(2)
        time_str = m.group(3)
        timestamp = f"{date_str}_{time_str}"

        # Parse CSV rows
        items = {}
        try:
            with open(csv_file, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    item_id = row.get("id")
                    if not item_id:
                        continue
                    items[item_id] = {
                        "name": row.get("db_name", ""),
                        "our_price": _safe_int(row.get("our_price_cents")),
                        "store_a_price": _safe_int(row.get(f"{STORE_A_COL}_converted_cents")),
                        "store_b_price": _safe_int(row.get(f"{STORE_B_COL}_converted_cents")),
                        "store_a_quality": row.get(f"{STORE_A_COL}_match_quality", ""),
                        "store_b_quality": row.get(f"{STORE_B_COL}_match_quality", ""),
                        "cheapest": row.get("cheapest", ""),
                    }
                    # Compute RRP from store prices
                    prices = []
                    if items[item_id]["store_a_price"] is not None:
                        prices.append(items[item_id]["store_a_price"])
                    if items[item_id]["store_b_price"] is not None:
                        prices.append(items[item_id]["store_b_price"])
                    items[item_id]["rrp"] = max(prices) if prices else None
        except Exception as e:
            logger.warning(f"Could not parse historical CSV {csv_file.name}: {e}")
            continue

        # Load matching results JSON for scrape health
        results_json_name = f"results_pipeline_{offer_id}_{timestamp}.json"
        results_json_path = output_dir / results_json_name
        summary = None
        if results_json_path.exists():
            try:
                with open(results_json_path, encoding="utf-8") as f:
                    data = json.load(f)
                summary = data.get("summary", {})
            except Exception as e:
                logger.warning(f"Could not read results JSON {results_json_name}: {e}")

        runs.append({
            "offer_id": offer_id,
            "date": date_str,
            "timestamp": timestamp,
            "items": items,
            "summary": summary,
        })

    # Sort chronologically, keep last 10
    runs.sort(key=lambda r: r["timestamp"])
    return runs[-10:]


def _safe_int(val) -> int | None:
    if not val or val == "-":
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return None


# ---------------------------------------------------------------------------
# Historical trend computation
# ---------------------------------------------------------------------------

def _compute_historical_trends(
    historical_runs: list[dict],
    current_run: dict | None,
) -> dict:
    """Compute trends across historical runs + current."""
    all_runs = historical_runs[:]
    if current_run:
        all_runs.append(current_run)

    if not all_runs:
        return {}

    # Per-run positioning trend
    positioning_trend = []
    cheapest_trend = []
    coverage_trend = []

    for run in all_runs:
        items = run["items"]
        below = above = equal = 0
        us = store_a = store_b = 0
        store_a_matches = store_b_matches = 0
        total = len(items)

        for item in items.values():
            our = item.get("our_price")
            rrp = item.get("rrp")
            if our is not None and rrp is not None and rrp > 0:
                if our < rrp:
                    below += 1
                elif our > rrp:
                    above += 1
                else:
                    equal += 1

            if item.get("store_a_price") is not None:
                store_a_matches += 1
            if item.get("store_b_price") is not None:
                store_b_matches += 1

            # Cheapest
            prices = {}
            if our is not None:
                prices["us"] = our
            if item.get("store_a_price") is not None:
                prices["store_a"] = item["store_a_price"]
            if item.get("store_b_price") is not None:
                prices["store_b"] = item["store_b_price"]
            if len(prices) >= 2:
                cheapest_key = min(prices, key=lambda k: prices[k])
                if cheapest_key == "us":
                    us += 1
                elif cheapest_key == "store_a":
                    store_a += 1
                else:
                    store_b += 1

        positioning_trend.append({
            "label": f"#{run['offer_id']} ({run['date']})",
            "below": below, "above": above, "equal": equal,
        })

        cheapest_total = us + store_a + store_b
        cheapest_trend.append({
            "label": f"#{run['offer_id']} ({run['date']})",
            "us": us, "store_a": store_a, "store_b": store_b,
            "total": cheapest_total,
        })

        coverage_trend.append({
            "label": f"#{run['offer_id']} ({run['date']})",
            "total": total,
            "store_a": store_a_matches,
            "store_b": store_b_matches,
        })

    # Scrape health trend (from results JSON summaries)
    health_trend = []
    for run in all_runs:
        summary = run.get("summary")
        if summary:
            total = summary.get("total_items", 0)
            sa_success = summary.get(f"{STORE_A_COL}_success", 0)
            sb_success = summary.get(f"{STORE_B_COL}_success", 0)
            health_trend.append({
                "label": f"#{run['offer_id']} ({run['date']})",
                "store_a_pct": (sa_success / total * 100) if total else 0,
                "store_b_pct": (sb_success / total * 100) if total else 0,
            })

    # Top volatile items (by stdev of RRP across runs)
    # Collect all item IDs and their RRP values across runs
    item_rrps = {}  # item_id -> [(run_label, rrp), ...]
    item_names = {}
    for run in all_runs:
        label = f"#{run['offer_id']} ({run['date']})"
        for item_id, item in run["items"].items():
            if item.get("rrp") is not None:
                if item_id not in item_rrps:
                    item_rrps[item_id] = []
                    item_names[item_id] = item["name"]
                item_rrps[item_id].append((label, item["rrp"]))

    # Merge by name to handle same product with different IDs across offers
    name_rrps: dict[str, dict[str, int]] = {}  # name -> {label: rrp}
    for item_id, points in item_rrps.items():
        name = item_names[item_id]
        if name not in name_rrps:
            name_rrps[name] = {}
        for label, rrp in points:
            name_rrps[name].setdefault(label, rrp)  # first-seen wins per label

    name_rrps_sorted: dict[str, list] = {
        name: sorted(pts.items(), key=lambda p: p[0])
        for name, pts in name_rrps.items()
    }

    # Filter to items appearing in 2+ runs, compute stdev
    volatile_items = []
    for name, points in name_rrps_sorted.items():
        if len(points) >= 2:
            values = [p[1] for p in points]
            sd = stdev(values)
            if sd > 0:
                volatile_items.append({
                    "name": name,
                    "stdev": sd,
                    "points": points,
                })

    volatile_items.sort(key=lambda x: x["stdev"], reverse=True)
    top_volatile = volatile_items[:10]

    all_items_prices = sorted(
        [{"name": name, "points": points} for name, points in name_rrps_sorted.items()],
        key=lambda x: x["name"],
    )

    # Price stability overview
    stable = moderate = volatile_count = 0
    for name, points in name_rrps_sorted.items():
        if len(points) >= 2:
            values = [p[1] for p in points]
            avg = sum(values) / len(values)
            if avg > 0:
                cv = stdev(values) / avg * 100  # coefficient of variation
                if cv < 5:
                    stable += 1
                elif cv < 15:
                    moderate += 1
                else:
                    volatile_count += 1

    return {
        "positioning_trend": positioning_trend,
        "cheapest_trend": cheapest_trend,
        "coverage_trend": coverage_trend,
        "health_trend": health_trend,
        "top_volatile": top_volatile,
        "all_items_prices": all_items_prices,
        "stability": {"stable": stable, "moderate": moderate, "volatile": volatile_count},
        "run_labels": [f"#{r['offer_id']} ({r['date']})" for r in all_runs],
    }


# ---------------------------------------------------------------------------
# Load input CSV search terms
# ---------------------------------------------------------------------------

def _load_search_terms(output_dir: Path, offer_id: int | None, timestamp: str | None) -> dict:
    """Load search terms from input CSV for the current run."""
    terms = {}
    if offer_id is None or timestamp is None:
        return terms

    # Try exact match first
    input_csv = output_dir / f"pipeline_input_{offer_id}_{timestamp}.csv"
    if not input_csv.exists():
        # Try dedup version
        input_csv = output_dir / f"pipeline_input_dedup_{offer_id}_{timestamp}.csv"
    if not input_csv.exists():
        # Fallback: glob for this offer
        candidates = sorted(output_dir.glob(f"pipeline_input_{offer_id}_*.csv"))
        if candidates:
            input_csv = candidates[-1]
        else:
            return terms

    try:
        with open(input_csv, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                item_id = row.get("id")
                search_term = row.get("search_term", "")
                if item_id and search_term:
                    terms[item_id] = search_term
    except Exception as e:
        logger.warning(f"Could not load search terms: {e}")

    return terms


# ---------------------------------------------------------------------------
# Build report data dict
# ---------------------------------------------------------------------------

def _build_report_data(
    updates: list,
    approved: list,
    csv_path: Path | None,
    offer_id: int | None,
    timestamp: str,
    settings_threshold: float,
    swot_data: dict | None,
    historical: dict,
    search_terms: dict,
) -> dict:
    """Assemble all data into a single dict for JSON serialization."""
    from scraper.wizard.steps.report import (
        _compute_price_positioning,
        _compute_store_coverage,
        _compute_cheapest,
        _compute_rrp_movement,
        _compute_outliers,
        _build_concerns,
        _load_csv_data,
    )

    csv_rows = _load_csv_data(csv_path) if csv_path and csv_path.exists() else []

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

    # Build executive summary
    good_count = sum(1 for u in approved if u.quality == "good")
    ok_count = sum(1 for u in approved if u.quality == "ok")
    poor_count = sum(1 for u in approved if u.quality == "poor")

    # Significant price differences
    sig_diffs = []
    for u in updates:
        if u.current_price == 0 or u.new_rrp == 0:
            continue
        diff_pct = ((u.current_price - u.new_rrp) / u.new_rrp) * 100
        if abs(diff_pct) >= settings_threshold:
            sig_diffs.append({
                "name": u.name, "our_price": u.current_price,
                "rrp": u.new_rrp, "diff_pct": round(diff_pct, 1),
            })
    sig_diffs.sort(key=lambda x: abs(x["diff_pct"]), reverse=True)

    # Major RRP changes
    rrp_changes = []
    for u in updates:
        if u.old_rrp is not None and u.old_rrp > 0 and u.old_rrp != u.new_rrp:
            diff_pct = ((u.new_rrp - u.old_rrp) / u.old_rrp) * 100
            if abs(diff_pct) >= settings_threshold:
                rrp_changes.append({
                    "name": u.name, "old_rrp": u.old_rrp,
                    "new_rrp": u.new_rrp, "diff_pct": round(diff_pct, 1),
                })
    rrp_changes.sort(key=lambda x: abs(x["diff_pct"]), reverse=True)

    # Outliers for JSON (convert tuples to dicts)
    outliers_json = {}
    for key in ("above_rrp", "below_rrp", "rrp_increases", "rrp_decreases"):
        outliers_json[key] = [
            {"name": name, "price_a": a, "price_b": b, "diff": d, "pct": round(pct, 1)}
            for name, a, b, d, pct in outliers.get(key, [])
        ]

    # Full item table
    full_items = []
    for u in updates:
        diff_pct = 0
        if u.new_rrp > 0:
            diff_pct = round(((u.current_price - u.new_rrp) / u.new_rrp) * 100, 1)
        full_items.append({
            "id": u.id,
            "name": u.name,
            "our_price": u.current_price,
            "new_rrp": u.new_rrp,
            "old_rrp": u.old_rrp,
            "store_a": u.store_a_price,
            "store_b": u.store_b_price,
            "quality": u.quality,
            "selected": u.id in {a.id for a in approved},
            "diff_pct": diff_pct,
            "search_term": search_terms.get(str(u.id), ""),
            "rrp_source": u.rrp_source,
        })

    return {
        "meta": {
            "offer_id": offer_id,
            "date": timestamp.split("_")[0] if "_" in timestamp else timestamp,
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "store_a_name": STORE_A_NAME,
            "store_b_name": STORE_B_NAME,
        },
        "summary": {
            "total_items": len(updates),
            "selected": len(approved),
            "good": good_count,
            "ok": ok_count,
            "poor": poor_count,
        },
        "threshold": settings_threshold,
        "sig_diffs": sig_diffs,
        "rrp_changes": rrp_changes,
        "positioning": positioning,
        "coverage": coverage,
        "cheapest": cheapest,
        "rrp_movement": rrp_mov,
        "outliers": outliers_json,
        "concerns": concerns,
        "swot": swot_data or {"strengths": [], "weaknesses": [], "opportunities": [], "threats": []},
        "historical": historical,
        "items": full_items,
    }


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = Template(r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Price Report #${offer_id} - ${date}</title>
<style>
:root {
  --primary: #2563eb;
  --success: #16a34a;
  --danger: #dc2626;
  --warning: #d97706;
  --info: #0891b2;
  --bg: #f8fafc;
  --card: #ffffff;
  --text: #1e293b;
  --text-dim: #64748b;
  --border: #e2e8f0;
  --shadow: 0 1px 3px rgba(0,0,0,0.1);
}
*, *::before, *::after { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
  font-size: 16px; line-height: 1.6; color: var(--text);
  background: var(--bg); margin: 0; padding: 0;
}
.layout { display: flex; max-width: 1400px; margin: 0 auto; }
nav.sidebar {
  position: sticky; top: 0; height: 100vh; width: 220px; min-width: 220px;
  overflow-y: auto; padding: 1rem 0.5rem; border-right: 1px solid var(--border);
  background: var(--card); font-size: 0.85rem;
}
nav.sidebar a {
  display: block; padding: 0.35rem 0.75rem; color: var(--text-dim);
  text-decoration: none; border-radius: 4px; margin-bottom: 2px;
}
nav.sidebar a:hover { background: var(--bg); color: var(--primary); }
nav.sidebar a.active { background: var(--bg); color: var(--primary); font-weight: 600; }
nav.sidebar .nav-title { font-weight: 700; color: var(--text); padding: 0.5rem 0.75rem; font-size: 0.9rem; }
main { flex: 1; min-width: 0; padding: 1.5rem 2rem; max-width: 1180px; }
h1 { font-size: 1.75rem; margin: 0 0 0.25rem; }
h2 { font-size: 1.25rem; margin: 1.5rem 0 0.75rem; padding-bottom: 0.35rem; border-bottom: 2px solid var(--primary); }
.subtitle { color: var(--text-dim); font-size: 0.9rem; margin-bottom: 1.5rem; }
.card { background: var(--card); border-radius: 8px; box-shadow: var(--shadow); padding: 1.25rem; margin-bottom: 1.25rem; }
.kpi-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }
.kpi { text-align: center; padding: 1rem; background: var(--card); border-radius: 8px; box-shadow: var(--shadow); }
.kpi-value { font-size: 2rem; font-weight: 700; }
.kpi-label { font-size: 0.8rem; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.05em; }
.kpi-green .kpi-value { color: var(--success); }
.kpi-amber .kpi-value { color: var(--warning); }
.kpi-red .kpi-value { color: var(--danger); }
.kpi-blue .kpi-value { color: var(--primary); }
.chart-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1.25rem; }
.chart-box { background: var(--card); border-radius: 8px; box-shadow: var(--shadow); padding: 1rem; }
.chart-box h3 { margin: 0 0 0.75rem; font-size: 1rem; }
.chart-box canvas { width: 100% !important; max-height: 280px; }

/* Tables */
table { width: 100%; border-collapse: collapse; font-size: 0.875rem; }
thead th {
  background: var(--bg); padding: 0.6rem 0.75rem; text-align: left;
  font-weight: 600; border-bottom: 2px solid var(--border); cursor: pointer;
  white-space: nowrap; position: sticky; top: 0; z-index: 1;
}
thead th:hover { background: #e2e8f0; }
thead th .sort-arrow { font-size: 0.7rem; margin-left: 4px; opacity: 0.4; }
thead th.sorted .sort-arrow { opacity: 1; }
tbody td { padding: 0.5rem 0.75rem; border-bottom: 1px solid var(--border); }
tbody tr:nth-child(even) { background: #f8fafc; }
tbody tr:hover { background: #eff6ff; }
.text-right { text-align: right; }
.text-center { text-align: center; }
.positive { color: var(--danger); }
.negative { color: var(--success); }
.neutral { color: var(--text-dim); }
.quality-good { color: var(--success); font-weight: 600; }
.quality-ok { color: var(--warning); font-weight: 600; }
.quality-poor { color: var(--danger); font-weight: 600; }
.badge-override { font-size: 0.7rem; background: #fef3c7; color: #92400e; border: 1px solid #fcd34d; border-radius: 3px; padding: 0 3px; margin-left: 4px; vertical-align: middle; cursor: default; }

/* Filter input */
.filter-row { margin-bottom: 0.75rem; }
.filter-row input {
  width: 100%; max-width: 400px; padding: 0.5rem 0.75rem;
  border: 1px solid var(--border); border-radius: 6px; font-size: 0.9rem;
}
.filter-row input:focus { outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px rgba(37,99,235,0.1); }

/* SWOT grid */
.swot-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
.swot-box { border-radius: 8px; padding: 1rem; border-left: 4px solid; }
.swot-box h3 { margin: 0 0 0.5rem; font-size: 1rem; }
.swot-box ul { margin: 0; padding-left: 1.25rem; }
.swot-box li { margin-bottom: 0.3rem; font-size: 0.9rem; }
.swot-strengths { background: #f0fdf4; border-color: var(--success); }
.swot-weaknesses { background: #fef2f2; border-color: var(--danger); }
.swot-opportunities { background: #ecfeff; border-color: var(--info); }
.swot-threats { background: #fff7ed; border-color: var(--warning); }

/* Preset buttons */
.preset-btn { padding: 0.3rem 0.6rem; margin-right: 0.35rem; margin-bottom: 0.35rem; font-size: 0.8rem; border: 1px solid var(--border); border-radius: 4px; cursor: pointer; background: var(--card); color: var(--text); }
.preset-btn.active { background: var(--primary); color: white; border-color: var(--primary); }
.preset-btn:hover:not(.active) { background: var(--bg); color: var(--primary); }

/* Outlier 2x2 grid */
.outlier-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }

/* Collapsible */
details { margin-bottom: 1rem; }
details summary { cursor: pointer; font-weight: 600; padding: 0.5rem 0; font-size: 1.1rem; }
details summary:hover { color: var(--primary); }

/* CSS-only fallback bars (shown when Chart.js fails) */
.fallback-bar-chart { display: none; }
.fallback-bar-chart .bar-row { display: flex; align-items: center; margin-bottom: 0.5rem; }
.fallback-bar-chart .bar-label { width: 120px; font-size: 0.85rem; flex-shrink: 0; }
.fallback-bar-chart .bar-track { flex: 1; height: 24px; background: #e2e8f0; border-radius: 4px; overflow: hidden; }
.fallback-bar-chart .bar-fill { height: 100%; border-radius: 4px; display: flex; align-items: center; padding-left: 6px; font-size: 0.75rem; color: white; font-weight: 600; }
.no-chartjs .fallback-bar-chart { display: block; }
.no-chartjs canvas { display: none; }

/* Responsive */
@media (max-width: 768px) {
  nav.sidebar { display: none; }
  main { padding: 1rem; }
  .chart-grid, .swot-grid, .outlier-grid { grid-template-columns: 1fr; }
  .kpi-row { grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); }
}

/* Print */
@media print {
  nav.sidebar { display: none; }
  main { padding: 0; max-width: 100%; }
  .card { box-shadow: none; border: 1px solid #ddd; page-break-inside: avoid; }
  .chart-box { page-break-inside: avoid; }
  h2 { page-break-after: avoid; }
}
</style>
</head>
<body>
<div class="layout">
<nav class="sidebar">
  <div class="nav-title">Report #${offer_id}</div>
  <a href="#summary">Executive Summary</a>
  <a href="#sig-diffs">Significant Diffs</a>
  <a href="#rrp-changes">RRP Changes</a>
  <a href="#positioning">Price Positioning</a>
  <a href="#coverage">Store Coverage</a>
  <a href="#cheapest">Who's Cheapest</a>
  <a href="#rrp-movement">RRP Movement</a>
  <a href="#outliers">Outliers</a>
  <a href="#swot">SWOT Analysis</a>
  <a href="#historical">Historical Trends</a>
  <a href="#items">All Items</a>
</nav>
<main>

<h1>Price Comparison Report</h1>
<p class="subtitle">Offer #${offer_id} &middot; ${date} &middot; Generated ${generated}</p>

<!-- Executive Summary -->
<section id="summary">
<h2>Executive Summary</h2>
<div class="kpi-row" id="kpi-row"></div>
</section>

<!-- Significant Price Differences -->
<section id="sig-diffs">
<h2>Significant Price Differences (&gt;${threshold}%)</h2>
<div class="card"><table id="sig-diffs-table"><thead><tr>
  <th data-sort="string">Item</th>
  <th data-sort="number" class="text-right">Our Price</th>
  <th data-sort="number" class="text-right">RRP</th>
  <th data-sort="number" class="text-right">Diff</th>
</tr></thead><tbody></tbody></table>
<p id="sig-diffs-empty" style="display:none;color:var(--text-dim);">No significant differences found.</p>
</div></section>

<!-- Major RRP Changes -->
<section id="rrp-changes">
<h2>Major RRP Changes</h2>
<div class="card"><table id="rrp-changes-table"><thead><tr>
  <th data-sort="string">Item</th>
  <th data-sort="number" class="text-right">Old RRP</th>
  <th data-sort="number" class="text-right">New RRP</th>
  <th data-sort="number" class="text-right">Change</th>
</tr></thead><tbody></tbody></table>
<p id="rrp-changes-empty" style="display:none;color:var(--text-dim);">No major RRP changes.</p>
</div></section>

<!-- Charts Grid: Positioning + Cheapest -->
<div class="chart-grid">
<section id="positioning" class="chart-box">
  <h3>Price Positioning vs RRP</h3>
  <canvas id="chart-positioning"></canvas>
  <div class="fallback-bar-chart" id="fb-positioning"></div>
  <div id="positioning-stats" style="margin-top:0.75rem;font-size:0.85rem;"></div>
</section>
<section id="cheapest" class="chart-box">
  <h3>Who's Cheapest</h3>
  <canvas id="chart-cheapest"></canvas>
  <div class="fallback-bar-chart" id="fb-cheapest"></div>
  <div id="cheapest-stats" style="margin-top:0.75rem;font-size:0.85rem;"></div>
</section>
</div>

<!-- Charts Grid: Coverage + RRP Movement -->
<div class="chart-grid" style="margin-top:1.25rem;">
<section id="coverage" class="chart-box">
  <h3>Store Coverage</h3>
  <canvas id="chart-coverage"></canvas>
  <div class="fallback-bar-chart" id="fb-coverage"></div>
  <div id="coverage-stats" style="margin-top:0.75rem;font-size:0.85rem;"></div>
</section>
<section id="rrp-movement" class="chart-box">
  <h3>RRP Movement</h3>
  <canvas id="chart-rrp-movement"></canvas>
  <div class="fallback-bar-chart" id="fb-rrp-movement"></div>
  <div id="rrp-movement-stats" style="margin-top:0.75rem;font-size:0.85rem;"></div>
</section>
</div>

<!-- Outliers -->
<section id="outliers">
<h2>Outliers</h2>
<div class="outlier-grid" id="outlier-grid"></div>
</section>

<!-- SWOT -->
<section id="swot">
<h2>SWOT Analysis</h2>
<div class="swot-grid" id="swot-grid"></div>
</section>

<!-- Historical -->
<section id="historical">
<h2>Historical Price Analysis</h2>
<div id="historical-content"></div>
</section>

<!-- Full Items Table -->
<section id="items">
<h2>All Items</h2>
<div class="card">
<div class="filter-row"><input type="text" id="item-filter" placeholder="Filter by name or search term..."></div>
<table id="items-table"><thead><tr>
  <th data-sort="number">ID</th>
  <th data-sort="string">Item</th>
  <th data-sort="string">Search Term</th>
  <th data-sort="number" class="text-right">Our Price</th>
  <th data-sort="number" class="text-right">New RRP</th>
  <th data-sort="number" class="text-right">Old RRP</th>
  <th data-sort="number" class="text-right">Diff%</th>
  <th data-sort="number" class="text-right">${store_a_name}</th>
  <th data-sort="number" class="text-right">${store_b_name}</th>
  <th data-sort="string" class="text-center">Quality</th>
  <th data-sort="string" class="text-center">Selected</th>
</tr></thead><tbody></tbody></table>
</div></section>

</main>
</div>

<script>
const DATA = ${json_data};
const SA_NAME = DATA.meta.store_a_name;
const SB_NAME = DATA.meta.store_b_name;

// Utility
function $(sel) { return document.querySelector(sel); }
function qsa(sel) { return document.querySelectorAll(sel); }
function fmt(cents) { return cents == null ? '-' : ('$' + (cents/100).toFixed(2)); }
function pct(v) { return v == null ? '-' : (v > 0 ? '+' : '') + v.toFixed(1) + '%'; }
function cls(v) { return v > 0 ? 'positive' : v < 0 ? 'negative' : 'neutral'; }
function esc(s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

// KPI cards
(function() {
  var s = DATA.summary, row = $('#kpi-row');
  var cards = [
    {v: s.total_items, l: 'Items Processed', c: 'blue'},
    {v: s.selected, l: 'Updates Selected', c: 'blue'},
    {v: s.good, l: 'Good Matches', c: 'green'},
    {v: s.ok, l: 'OK Matches', c: 'amber'},
    {v: s.poor, l: 'Poor Matches', c: 'red'},
  ];
  cards.forEach(function(c) {
    row.innerHTML += '<div class="kpi kpi-' + c.c + '"><div class="kpi-value">' + c.v + '</div><div class="kpi-label">' + c.l + '</div></div>';
  });
})();

// Significant diffs table
(function() {
  var tbody = $('#sig-diffs-table tbody'), rows = DATA.sig_diffs;
  if (!rows.length) { $('#sig-diffs-empty').style.display = 'block'; $('#sig-diffs-table').style.display = 'none'; return; }
  rows.forEach(function(r) {
    tbody.innerHTML += '<tr><td>' + esc(r.name) + '</td><td class="text-right">' + fmt(r.our_price) +
      '</td><td class="text-right">' + fmt(r.rrp) + '</td><td class="text-right ' + cls(r.diff_pct) + '">' + pct(r.diff_pct) + '</td></tr>';
  });
})();

// RRP changes table
(function() {
  var tbody = $('#rrp-changes-table tbody'), rows = DATA.rrp_changes;
  if (!rows.length) { $('#rrp-changes-empty').style.display = 'block'; $('#rrp-changes-table').style.display = 'none'; return; }
  rows.forEach(function(r) {
    tbody.innerHTML += '<tr><td>' + esc(r.name) + '</td><td class="text-right">' + fmt(r.old_rrp) +
      '</td><td class="text-right">' + fmt(r.new_rrp) + '</td><td class="text-right ' + cls(r.diff_pct) + '">' + pct(r.diff_pct) + '</td></tr>';
  });
})();

// Positioning stats
(function() {
  var p = DATA.positioning, el = $('#positioning-stats');
  if (p.total === 0) { el.textContent = 'No data'; return; }
  el.innerHTML = '<b>Below:</b> ' + p.below + ' (' + (p.below/p.total*100).toFixed(1) + '%) &middot; ' +
    '<b>Above:</b> ' + p.above + ' (' + (p.above/p.total*100).toFixed(1) + '%) &middot; ' +
    '<b>Equal:</b> ' + p.equal +
    '<br><b>Avg margin:</b> <span class="' + cls(p.avg_margin) + '">' + (p.avg_margin >= 0 ? '+' : '') + p.avg_margin.toFixed(1) + '%</span>' +
    ' &middot; <b>Median:</b> <span class="' + cls(p.med_margin) + '">' + (p.med_margin >= 0 ? '+' : '') + p.med_margin.toFixed(1) + '%</span>';
})();

// Cheapest stats
(function() {
  var c = DATA.cheapest, el = $('#cheapest-stats');
  if (c.total === 0) { el.textContent = 'No comparable items'; return; }
  el.innerHTML = '<b>Us:</b> ' + c.us + ' (' + (c.us/c.total*100).toFixed(1) + '%) &middot; ' +
    '<b>' + esc(SA_NAME) + ':</b> ' + c.store_a + ' &middot; <b>' + esc(SB_NAME) + ':</b> ' + c.store_b +
    ' (of ' + c.total + ')';
})();

// Coverage stats
(function() {
  var c = DATA.coverage, el = $('#coverage-stats');
  if (c.total === 0) { el.textContent = 'No data'; return; }
  el.innerHTML = '<b>' + esc(SA_NAME) + ':</b> ' + c.store_a_matches + '/' + c.total + ' matched' +
    ' (G:' + c.store_a_good + ' O:' + c.store_a_ok + ' P:' + c.store_a_poor + ')' +
    '<br><b>' + esc(SB_NAME) + ':</b> ' + c.store_b_matches + '/' + c.total + ' matched' +
    ' (G:' + c.store_b_good + ' O:' + c.store_b_ok + ' P:' + c.store_b_poor + ')' +
    '<br><b>Both:</b> ' + c.both + ' &middot; <b>Neither:</b> ' + c.neither + ' &middot; <b>Single:</b> ' + c.single;
})();

// RRP movement stats
(function() {
  var m = DATA.rrp_movement, el = $('#rrp-movement-stats');
  if (m.total === 0) { el.textContent = 'No data'; return; }
  el.innerHTML = '<b>Increased:</b> ' + m.increased + ' (avg ' + fmt(Math.round(m.avg_inc)) + ')' +
    ' &middot; <b>Decreased:</b> ' + m.decreased + ' (avg ' + fmt(Math.round(m.avg_dec)) + ')' +
    ' &middot; <b>Unchanged:</b> ' + m.unchanged;
})();

// Outliers
(function() {
  var grid = $('#outlier-grid');
  var nameToSelected = {};
  DATA.items.forEach(function(item) { nameToSelected[item.name] = item.selected; });
  var sections = [
    {key: 'above_rrp', title: 'Most Expensive vs RRP', cols: ['Item','Ours','RRP','Diff','Sel'], color: 'positive', diffStyle: null, sign: '+'},
    {key: 'below_rrp', title: 'Best Value vs RRP', cols: ['Item','Ours','RRP','Diff','Sel'], color: 'negative', diffStyle: null, sign: '-'},
    {key: 'rrp_increases', title: 'Biggest RRP Increases', cols: ['Item','Old RRP','New RRP','Change','Sel'], color: null, diffStyle: 'color:#0891b2', sign: '+'},
    {key: 'rrp_decreases', title: 'Biggest RRP Decreases', cols: ['Item','Old RRP','New RRP','Change','Sel'], color: null, diffStyle: 'color:#d97706', sign: '-'},
  ];
  sections.forEach(function(sec) {
    var items = DATA.outliers[sec.key];
    if (!items || !items.length) return;
    var html = '<div class="card"><h3>' + sec.title + '</h3><table><thead><tr>';
    sec.cols.forEach(function(c,i) {
      if (c === 'Sel') html += '<th class="text-center" style="width:3rem;">Sel</th>';
      else html += '<th' + (i>0?' class="text-right"':'') + '>' + c + '</th>';
    });
    html += '</tr></thead><tbody>';
    items.forEach(function(r) {
      var diffCell = sec.diffStyle
        ? '<td class="text-right" style="' + sec.diffStyle + '">' + sec.sign + Math.abs(r.pct).toFixed(1) + '%</td>'
        : '<td class="text-right ' + sec.color + '">' + sec.sign + Math.abs(r.pct).toFixed(1) + '%</td>';
      var selCell = '<td class="text-center">' + (nameToSelected[r.name] ? '\u2705' : '\u274c') + '</td>';
      html += '<tr><td>' + esc(r.name) + '</td><td class="text-right">' + fmt(r.price_a) +
        '</td><td class="text-right">' + fmt(r.price_b) + '</td>' + diffCell + selCell + '</tr>';
    });
    html += '</tbody></table></div>';
    grid.innerHTML += html;
  });
  if (!grid.innerHTML.trim()) grid.innerHTML = '<p style="color:var(--text-dim);">No outliers found.</p>';
})();

// SWOT
(function() {
  var grid = $('#swot-grid'), sw = DATA.swot;
  var quads = [
    {key: 'strengths', title: 'Strengths', cls: 'swot-strengths'},
    {key: 'weaknesses', title: 'Weaknesses', cls: 'swot-weaknesses'},
    {key: 'opportunities', title: 'Opportunities', cls: 'swot-opportunities'},
    {key: 'threats', title: 'Threats', cls: 'swot-threats'},
  ];
  quads.forEach(function(q) {
    var items = sw[q.key] || [];
    var html = '<div class="swot-box ' + q.cls + '"><h3>' + q.title + '</h3>';
    if (items.length) {
      html += '<ul>';
      items.forEach(function(i) { html += '<li>' + esc(i) + '</li>'; });
      html += '</ul>';
    } else {
      html += '<p style="color:var(--text-dim);font-size:0.9rem;">No significant factors identified.</p>';
    }
    grid.innerHTML += html + '</div>';
  });
})();

// Historical section
(function() {
  var el = $('#historical-content'), h = DATA.historical;
  if (!h || !h.run_labels || h.run_labels.length < 2) {
    el.innerHTML = '<p style="color:var(--text-dim);">Not enough historical data (need 2+ runs).</p>';
    return;
  }

  var html = '<div class="chart-grid">';
  html += '<div class="chart-box"><h3>Positioning Trend</h3><canvas id="chart-hist-positioning"></canvas><div class="fallback-bar-chart" id="fb-hist-positioning"></div></div>';
  html += '<div class="chart-box"><h3>Scrape Health</h3><canvas id="chart-hist-health"></canvas><div class="fallback-bar-chart" id="fb-hist-health"></div></div>';
  html += '</div>';

  // Interactive Price Trend Explorer
  if ((h.all_items_prices && h.all_items_prices.length) || (h.top_volatile && h.top_volatile.length)) {
    html += '<div class="chart-box" style="margin-top:1.25rem;" id="volatile-explorer">';
    html += '<h3>Price Trend Explorer</h3>';
    html += '<div id="volatile-presets" style="margin-bottom:0.5rem;">';
    html += '<button class="preset-btn active" data-preset="volatile">Most Volatile</button>';
    html += '<button class="preset-btn" data-preset="expensive">Most Expensive vs RRP</button>';
    html += '<button class="preset-btn" data-preset="value">Best Value vs RRP</button>';
    html += '<button class="preset-btn" data-preset="inc">Biggest RRP Increases</button>';
    html += '<button class="preset-btn" data-preset="dec">Biggest RRP Decreases</button>';
    html += '</div>';
    html += '<input type="text" id="volatile-filter" placeholder="Filter items..." style="width:100%;max-width:350px;padding:0.35rem 0.6rem;border:1px solid var(--border);border-radius:4px;font-size:0.85rem;margin-bottom:0.4rem;">';
    html += '<div id="volatile-checklist" style="max-height:150px;overflow-y:auto;border:1px solid var(--border);border-radius:4px;padding:0.25rem 0.5rem;margin-bottom:0.75rem;font-size:0.85rem;"></div>';
    html += '<canvas id="chart-volatile" style="max-height:350px;"></canvas>';
    html += '<p style="font-size:0.8rem;color:var(--text-dim);margin-top:0.4rem;">Shows up to 12 items. Lines begin at first run with competitor price data.</p>';
    html += '</div>';
  }

  // Price stability overview
  if (h.stability) {
    var st = h.stability;
    html += '<div class="card" style="margin-top:1.25rem;"><h3>Price Stability Overview</h3>';
    html += '<div class="kpi-row">';
    html += '<div class="kpi kpi-green"><div class="kpi-value">' + st.stable + '</div><div class="kpi-label">Stable (&lt;5% CV)</div></div>';
    html += '<div class="kpi kpi-amber"><div class="kpi-value">' + st.moderate + '</div><div class="kpi-label">Moderate (5-15%)</div></div>';
    html += '<div class="kpi kpi-red"><div class="kpi-value">' + st.volatile + '</div><div class="kpi-label">Volatile (&gt;15%)</div></div>';
    html += '</div></div>';
  }

  // Top volatile items table
  if (h.top_volatile && h.top_volatile.length) {
    html += '<details style="margin-top:1rem;"><summary>Top Volatile Items Detail</summary><div class="card"><table><thead><tr><th>Item</th><th class="text-right">Stdev (cents)</th><th>RRP History</th></tr></thead><tbody>';
    h.top_volatile.forEach(function(v) {
      var hist = v.points.map(function(p) { return '<div>' + esc(p[0]) + ': ' + fmt(p[1]) + '</div>'; }).join('');
      html += '<tr><td>' + esc(v.name) + '</td><td class="text-right">' + v.stdev.toFixed(0) + 'c</td><td style="font-size:0.8rem;">' + hist + '</td></tr>';
    });
    html += '</tbody></table></div></details>';
  }

  el.innerHTML = html;
})();

// Full items table
(function() {
  var tbody = $('#items-table tbody');
  DATA.items.forEach(function(r) {
    var qcls = 'quality-' + r.quality;
    tbody.innerHTML += '<tr>' +
      '<td>' + r.id + '</td>' +
      '<td>' + esc(r.name) + '</td>' +
      '<td>' + esc(r.search_term) + '</td>' +
      '<td class="text-right">' + fmt(r.our_price) + '</td>' +
      '<td class="text-right">' + fmt(r.new_rrp) + (r.rrp_source ? '<span class="badge-override" title="RRP overridden (' + r.rrp_source + ')">✎</span>' : '') + '</td>' +
      '<td class="text-right">' + fmt(r.old_rrp) + '</td>' +
      '<td class="text-right ' + cls(r.diff_pct) + '">' + pct(r.diff_pct) + '</td>' +
      '<td class="text-right">' + fmt(r.store_a) + '</td>' +
      '<td class="text-right">' + fmt(r.store_b) + '</td>' +
      '<td class="text-center ' + qcls + '">' + r.quality + '</td>' +
      '<td class="text-center">' + (r.selected ? 'Yes' : 'No') + '</td>' +
      '</tr>';
  });
})();

// Filter
$('#item-filter').addEventListener('input', function() {
  var val = this.value.toLowerCase();
  qsa('#items-table tbody tr').forEach(function(row) {
    var text = row.cells[1].textContent.toLowerCase() + ' ' + row.cells[2].textContent.toLowerCase();
    row.style.display = text.indexOf(val) >= 0 ? '' : 'none';
  });
});

// Sortable tables
(function() {
  qsa('table').forEach(function(table) {
    var headers = table.querySelectorAll('thead th[data-sort]');
    if (!headers.length) return;
    headers.forEach(function(th, colIdx) {
      th.innerHTML += ' <span class="sort-arrow">&#9650;</span>';
      th.addEventListener('click', function() {
        var tbody = table.querySelector('tbody');
        var rows = Array.from(tbody.querySelectorAll('tr')).filter(function(r) { return r.style.display !== 'none' || true; });
        var type = th.dataset.sort;
        var asc = th.classList.contains('sorted-asc');
        // Reset all headers
        table.querySelectorAll('thead th').forEach(function(h) { h.classList.remove('sorted','sorted-asc','sorted-desc'); });
        th.classList.add('sorted', asc ? 'sorted-desc' : 'sorted-asc');
        th.querySelector('.sort-arrow').innerHTML = asc ? '&#9660;' : '&#9650;';

        rows.sort(function(a, b) {
          var av = a.cells[colIdx].textContent.trim();
          var bv = b.cells[colIdx].textContent.trim();
          if (type === 'number') {
            av = parseFloat(av.replace(/[$,%+]/g, '')) || 0;
            bv = parseFloat(bv.replace(/[$,%+]/g, '')) || 0;
          }
          if (av < bv) return asc ? 1 : -1;
          if (av > bv) return asc ? -1 : 1;
          return 0;
        });
        rows.forEach(function(r) { tbody.appendChild(r); });
      });
    });
  });
})();

// Smooth scrolling for sidebar nav
qsa('nav.sidebar a').forEach(function(a) {
  a.addEventListener('click', function(e) {
    e.preventDefault();
    var target = document.querySelector(this.getAttribute('href'));
    if (target) target.scrollIntoView({behavior: 'smooth', block: 'start'});
  });
});

// Scrollspy
(function() {
  var navLinks = {};
  qsa('nav.sidebar a[href^="#"]').forEach(function(a) {
    navLinks[a.getAttribute('href').substring(1)] = a;
  });
  var observer = new IntersectionObserver(function(entries) {
    entries.forEach(function(entry) {
      if (entry.isIntersecting) {
        Object.keys(navLinks).forEach(function(k) { navLinks[k].classList.remove('active'); });
        var link = navLinks[entry.target.id];
        if (link) link.classList.add('active');
      }
    });
  }, { rootMargin: '-20% 0px -70% 0px' });
  qsa('main section[id]').forEach(function(s) { observer.observe(s); });
})();

// Volatile chart explorer (requires Chart.js)
var volatileChart = null;
function initVolatileExplorer() {
  var H = DATA.historical;
  if (!H || (!H.all_items_prices && !H.top_volatile)) return;
  var allItems = H.all_items_prices || [];
  if (!allItems.length && H.top_volatile) allItems = H.top_volatile;
  var colors = ['#2563eb','#dc2626','#16a34a','#d97706','#0891b2','#7c3aed','#db2777','#059669','#ea580c','#4f46e5'];

  var nameToItem = {};
  allItems.forEach(function(item) { nameToItem[item.name] = item; });

  function getPresetItems(preset) {
    if (preset === 'volatile') {
      return (H.top_volatile || []).slice(0, 12);
    }
    if (preset === 'expensive') {
      var seen = {}, names = [];
      (DATA.outliers.above_rrp || []).forEach(function(r) { if (!seen[r.name]) { seen[r.name] = true; names.push(r.name); } });
      (DATA.sig_diffs || []).filter(function(r) { return r.diff_pct > 0; }).sort(function(a,b) { return b.diff_pct - a.diff_pct; }).forEach(function(r) { if (!seen[r.name]) { seen[r.name] = true; names.push(r.name); } });
      return names.slice(0, 12).map(function(n) { return nameToItem[n]; }).filter(Boolean);
    }
    if (preset === 'value') {
      return (DATA.outliers.below_rrp || []).slice(0, 12).map(function(r) { return nameToItem[r.name]; }).filter(Boolean);
    }
    if (preset === 'inc') {
      return (DATA.rrp_changes || []).filter(function(r) { return r.diff_pct > 0; }).sort(function(a,b) { return b.diff_pct - a.diff_pct; }).slice(0, 12).map(function(r) { return nameToItem[r.name]; }).filter(Boolean);
    }
    if (preset === 'dec') {
      return (DATA.rrp_changes || []).filter(function(r) { return r.diff_pct < 0; }).sort(function(a,b) { return a.diff_pct - b.diff_pct; }).slice(0, 12).map(function(r) { return nameToItem[r.name]; }).filter(Boolean);
    }
    return [];
  }

  var checkedNames = new Set(getPresetItems('volatile').map(function(i) { return i.name; }));

  function buildChecklist(filterText) {
    var el = document.getElementById('volatile-checklist');
    if (!el) return;
    var html = '';
    allItems.forEach(function(item) {
      if (filterText && item.name.toLowerCase().indexOf(filterText) < 0) return;
      html += '<label style="display:block;padding:2px 4px;cursor:pointer;"><input type="checkbox" value="' + esc(item.name) + '"' + (checkedNames.has(item.name) ? ' checked' : '') + '> ' + esc(item.name) + '</label>';
    });
    el.innerHTML = html || '<span style="color:var(--text-dim);">No items match.</span>';
    el.querySelectorAll('input[type=checkbox]').forEach(function(cb) {
      cb.addEventListener('change', function() {
        if (this.checked && checkedNames.size >= 12) { this.checked = false; return; }
        if (this.checked) checkedNames.add(this.value);
        else checkedNames.delete(this.value);
        drawVolatileChart();
      });
    });
  }

  function drawVolatileChart() {
    var canvas = document.getElementById('chart-volatile');
    if (!canvas) return;
    var selected = allItems.filter(function(i) { return checkedNames.has(i.name); }).slice(0, 12);
    var datasets = selected.map(function(v, i) {
      var dataMap = {};
      v.points.forEach(function(p) { dataMap[p[0]] = p[1]; });
      return {
        label: v.name.substring(0, 25),
        data: H.run_labels.map(function(l) { return dataMap[l] != null ? dataMap[l] / 100 : null; }),
        borderColor: colors[i % colors.length],
        fill: false, tension: 0.3, spanGaps: true,
      };
    });
    if (volatileChart) {
      volatileChart.data.datasets = datasets;
      volatileChart.update();
    } else {
      volatileChart = new Chart(canvas, {
        type: 'line',
        data: { labels: H.run_labels, datasets: datasets },
        options: {
          responsive: true,
          scales: { y: { ticks: { callback: function(v) { return '$' + v.toFixed(2); } } } },
          plugins: { legend: { position: 'bottom', labels: { boxWidth: 12 } } },
        },
      });
    }
  }

  document.querySelectorAll('.preset-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
      document.querySelectorAll('.preset-btn').forEach(function(b) { b.classList.remove('active'); });
      this.classList.add('active');
      var items = getPresetItems(this.dataset.preset);
      checkedNames = new Set(items.map(function(i) { return i.name; }));
      var filterEl = document.getElementById('volatile-filter');
      buildChecklist(filterEl ? filterEl.value.toLowerCase() : '');
      drawVolatileChart();
    });
  });

  var filterEl = document.getElementById('volatile-filter');
  if (filterEl) filterEl.addEventListener('input', function() { buildChecklist(this.value.toLowerCase()); });

  buildChecklist('');
  drawVolatileChart();
}

// Chart.js rendering
var chartjsLoaded = false;
function renderCharts() {
  chartjsLoaded = true;
  var P = DATA.positioning, C = DATA.cheapest, COV = DATA.coverage, M = DATA.rrp_movement;

  // Positioning donut
  if (P.total > 0) {
    new Chart($('#chart-positioning'), {
      type: 'doughnut',
      data: { labels: ['Below RRP','Above RRP','Equal'], datasets: [{ data: [P.below, P.above, P.equal], backgroundColor: ['#16a34a','#dc2626','#64748b'] }] },
      options: { responsive: true, plugins: { legend: { position: 'bottom' } } }
    });
  }

  // Cheapest donut
  if (C.total > 0) {
    new Chart($('#chart-cheapest'), {
      type: 'doughnut',
      data: { labels: ['Us', SA_NAME, SB_NAME], datasets: [{ data: [C.us, C.store_a, C.store_b], backgroundColor: ['#16a34a','#dc2626','#d97706'] }] },
      options: { responsive: true, plugins: { legend: { position: 'bottom' } } }
    });
  }

  // Coverage horizontal bar
  if (COV.total > 0) {
    new Chart($('#chart-coverage'), {
      type: 'bar',
      data: {
        labels: [SA_NAME, SB_NAME],
        datasets: [
          { label: 'Good', data: [COV.store_a_good, COV.store_b_good], backgroundColor: '#16a34a' },
          { label: 'OK', data: [COV.store_a_ok, COV.store_b_ok], backgroundColor: '#d97706' },
          { label: 'Poor', data: [COV.store_a_poor, COV.store_b_poor], backgroundColor: '#dc2626' },
        ]
      },
      options: { indexAxis: 'y', responsive: true, scales: { x: { stacked: true }, y: { stacked: true } }, plugins: { legend: { position: 'bottom' } } }
    });
  }

  // RRP Movement bar
  if (M.total > 0) {
    new Chart($('#chart-rrp-movement'), {
      type: 'bar',
      data: {
        labels: ['Increased','Decreased','Unchanged'],
        datasets: [{ data: [M.increased, M.decreased, M.unchanged], backgroundColor: ['#0891b2','#d97706','#64748b'] }]
      },
      options: { responsive: true, plugins: { legend: { display: false } } }
    });
  }

  // Historical charts
  var H = DATA.historical;
  if (H && H.run_labels && H.run_labels.length >= 2) {
    // Positioning trend (stacked bar)
    var posCanvas = document.getElementById('chart-hist-positioning');
    if (posCanvas && H.positioning_trend) {
      new Chart(posCanvas, {
        type: 'bar',
        data: {
          labels: H.positioning_trend.map(function(r) { return r.label; }),
          datasets: [
            { label: 'Below', data: H.positioning_trend.map(function(r) { return r.below; }), backgroundColor: '#16a34a' },
            { label: 'Above', data: H.positioning_trend.map(function(r) { return r.above; }), backgroundColor: '#dc2626' },
            { label: 'Equal', data: H.positioning_trend.map(function(r) { return r.equal; }), backgroundColor: '#64748b' },
          ]
        },
        options: { responsive: true, scales: { x: { stacked: true }, y: { stacked: true } }, plugins: { legend: { position: 'bottom' } } }
      });
    }

    // Scrape health trend (line)
    var healthCanvas = document.getElementById('chart-hist-health');
    if (healthCanvas && H.health_trend && H.health_trend.length) {
      new Chart(healthCanvas, {
        type: 'line',
        data: {
          labels: H.health_trend.map(function(r) { return r.label; }),
          datasets: [
            { label: SA_NAME + ' success %', data: H.health_trend.map(function(r) { return r.store_a_pct.toFixed(1); }), borderColor: '#2563eb', fill: false, tension: 0.3 },
            { label: SB_NAME + ' success %', data: H.health_trend.map(function(r) { return r.store_b_pct.toFixed(1); }), borderColor: '#16a34a', fill: false, tension: 0.3 },
          ]
        },
        options: { responsive: true, scales: { y: { min: 0, max: 100 } }, plugins: { legend: { position: 'bottom' } } }
      });
    }

    initVolatileExplorer();
  }
}

// Fallback bars
function renderFallbackBars() {
  document.body.classList.add('no-chartjs');
  var P = DATA.positioning, C = DATA.cheapest, COV = DATA.coverage, M = DATA.rrp_movement;

  function barHTML(items, maxVal) {
    return items.map(function(b) {
      var w = maxVal > 0 ? (b.val / maxVal * 100) : 0;
      return '<div class="bar-row"><div class="bar-label">' + b.label + '</div><div class="bar-track"><div class="bar-fill" style="width:' + w + '%;background:' + b.color + ';">' + b.val + '</div></div></div>';
    }).join('');
  }

  if (P.total > 0) {
    var mx = Math.max(P.below, P.above, P.equal);
    $('#fb-positioning').innerHTML = barHTML([{label:'Below',val:P.below,color:'#16a34a'},{label:'Above',val:P.above,color:'#dc2626'},{label:'Equal',val:P.equal,color:'#64748b'}], mx);
  }
  if (C.total > 0) {
    var mx2 = Math.max(C.us, C.store_a, C.store_b);
    $('#fb-cheapest').innerHTML = barHTML([{label:'Us',val:C.us,color:'#16a34a'},{label:SA_NAME,val:C.store_a,color:'#dc2626'},{label:SB_NAME,val:C.store_b,color:'#d97706'}], mx2);
  }
  if (COV.total > 0) {
    var mx3 = Math.max(COV.store_a_matches, COV.store_b_matches);
    $('#fb-coverage').innerHTML = barHTML([{label:SA_NAME,val:COV.store_a_matches,color:'#2563eb'},{label:SB_NAME,val:COV.store_b_matches,color:'#16a34a'}], mx3);
  }
  if (M.total > 0) {
    var mx4 = Math.max(M.increased, M.decreased, M.unchanged);
    $('#fb-rrp-movement').innerHTML = barHTML([{label:'Increased',val:M.increased,color:'#0891b2'},{label:'Decreased',val:M.decreased,color:'#d97706'},{label:'Unchanged',val:M.unchanged,color:'#64748b'}], mx4);
  }
}

// Load Chart.js from CDN, fallback on error
(function() {
  var script = document.createElement('script');
  script.src = 'https://cdn.jsdelivr.net/npm/chart.js@4';
  script.onload = function() { renderCharts(); };
  script.onerror = function() { renderFallbackBars(); };
  document.head.appendChild(script);
})();
</script>
</body>
</html>""")


def _render_html(report_data: dict) -> str:
    """Render the complete HTML report string."""
    meta = report_data["meta"]
    return _HTML_TEMPLATE.safe_substitute(
        offer_id=meta.get("offer_id", "?"),
        date=meta.get("date", ""),
        generated=meta.get("generated", ""),
        threshold=report_data.get("threshold", 25),
        store_a_name=meta.get("store_a_name", "Store A"),
        store_b_name=meta.get("store_b_name", "Store B"),
        json_data=json.dumps(report_data, default=str),
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_html_report(
    updates: list,
    approved: list,
    comparison_csv_path: "Path | None",
    offer_id: int | None,
    timestamp: str,
    output_dir: Path,
    settings_threshold: float = 25.0,
    swot_data: dict | None = None,
) -> Path | None:
    """
    Generate a self-contained HTML report.

    Args:
        updates: All candidate UpdateRows.
        approved: Selected/approved UpdateRows.
        comparison_csv_path: Path to comparison CSV (for store coverage stats).
        offer_id: Offer ID being processed.
        timestamp: Run timestamp string (YYYY-MM-DD_HHMMSS).
        output_dir: Where to write the report.
        settings_threshold: Significant price diff threshold %.
        swot_data: Pre-computed SWOT dict (avoids duplicate LLM call).

    Returns:
        Path to generated HTML file, or None on error.
    """
    try:
        output_dir.mkdir(parents=True, exist_ok=True)

        # Load historical data
        historical_runs = _load_historical_data(output_dir, comparison_csv_path)

        # Build current run snapshot for historical trends
        current_run = None
        if comparison_csv_path and comparison_csv_path.exists():
            # Extract offer_id and date from the current run
            m = re.search(r"(\d+)_(\d{4}-\d{2}-\d{2})_(\d{6})", str(comparison_csv_path.name))
            date_str = m.group(2) if m else timestamp.split("_")[0]
            time_str = m.group(3) if m else timestamp.replace("-", "").replace("_", "")[:6]

            items = {}
            try:
                with open(comparison_csv_path, encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        item_id = row.get("id")
                        if not item_id:
                            continue
                        sa_price = _safe_int(row.get(f"{STORE_A_COL}_converted_cents"))
                        sb_price = _safe_int(row.get(f"{STORE_B_COL}_converted_cents"))
                        prices = [p for p in [sa_price, sb_price] if p is not None]
                        items[item_id] = {
                            "name": row.get("db_name", ""),
                            "our_price": _safe_int(row.get("our_price_cents")),
                            "store_a_price": sa_price,
                            "store_b_price": sb_price,
                            "rrp": max(prices) if prices else None,
                        }
            except Exception as e:
                logger.warning(f"Could not parse current CSV for historical: {e}")

            # Load results JSON summary for current run
            results_json_name = f"results_pipeline_{offer_id}_{date_str}_{time_str}.json"
            results_json_path = output_dir / results_json_name
            summary = None
            if results_json_path.exists():
                try:
                    with open(results_json_path, encoding="utf-8") as f:
                        data = json.load(f)
                    summary = data.get("summary", {})
                except Exception:
                    pass

            current_run = {
                "offer_id": offer_id or 0,
                "date": date_str,
                "timestamp": f"{date_str}_{time_str}",
                "items": items,
                "summary": summary,
            }

        historical = _compute_historical_trends(historical_runs, current_run)

        # Load search terms from input CSV
        search_terms = _load_search_terms(output_dir, offer_id, timestamp)

        # Build full report data
        report_data = _build_report_data(
            updates=updates,
            approved=approved,
            csv_path=comparison_csv_path,
            offer_id=offer_id,
            timestamp=timestamp,
            settings_threshold=settings_threshold,
            swot_data=swot_data,
            historical=historical,
            search_terms=search_terms,
        )

        # Render and write
        html = _render_html(report_data)
        report_path = output_dir / f"report_{offer_id}_{timestamp}.html"
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(html)

        logger.info(f"HTML report generated: {report_path}")
        return report_path

    except Exception as e:
        logger.error(f"Failed to generate HTML report: {e}")
        return None
