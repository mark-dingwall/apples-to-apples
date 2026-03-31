"""
Automated price comparison pipeline.

Orchestrates:
1. Query DB for items
2. Generate search terms (Claude CLI)
3. Scrape prices
4. Process results (Claude CLI)
5. TUI approval
6. Update DB with RRP
"""

import argparse
import csv
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from scraper.db import OfferPart, execute_updates, fetch_items, verify_offer_exists
from scraper.stores.store_config import STORE_A_NAME, STORE_B_NAME, STORE_A_COL, STORE_B_COL
from scraper.tui import UpdateRow, show_approval_tui, show_summary
from scraper.utils.claude_cli import call_claude_cli
from scraper.wizard.settings import SettingsManager

try:
    from scraper.prompts import SEARCH_TERM_PROMPT
except ImportError:
    raise ImportError(
        "Prompt templates not found. Copy scraper/prompts.example.py "
        "to scraper/prompts.py and customise for your use case."
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class ComparisonResult:
    """Parsed result from processor CSV."""
    id: int
    db_name: str
    our_price_cents: int
    store_a_converted_cents: int | None
    store_a_match_quality: str
    store_a_qty_multiplier: float = 1.0
    store_b_converted_cents: int | None = None
    store_b_match_quality: str = "none"
    store_b_qty_multiplier: float = 1.0


def generate_search_terms_batch(items: list[dict], batch_size: int = 15) -> tuple[dict[int, str], list[int]]:
    """
    Generate search terms for items using Claude CLI.

    Args:
        items: List of dicts with 'id' and 'name' keys
        batch_size: Items per CLI call

    Returns:
        Tuple of (dict mapping item_id to search_term, list of failed item_ids)
    """
    all_terms = {}
    failed_items: list[int] = []

    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (len(items) + batch_size - 1) // batch_size
        batch_ids = [item["id"] for item in batch]

        logger.info(f"Generating search terms batch {batch_num}/{total_batches}...")

        items_json = json.dumps([{"id": item["id"], "name": item["name"]} for item in batch])

        prompt = SEARCH_TERM_PROMPT.format(items_json=items_json)

        response = call_claude_cli(prompt)

        if not response:
            logger.warning(f"Failed to get search terms for batch {batch_num} ({len(batch)} items)")
            failed_items.extend(batch_ids)
            continue

        try:
            # Extract JSON from response
            json_match = re.search(r"\{[\s\S]*\}", response)
            if not json_match:
                logger.warning(f"No JSON found in response for batch {batch_num}")
                failed_items.extend(batch_ids)
                continue

            data = json.loads(json_match.group())
            terms = data.get("terms", [])

            # Track which items we got terms for
            received_ids = set()
            for term_data in terms:
                item_id = term_data.get("id")
                search_term = term_data.get("search_term", "")
                if item_id and search_term:
                    all_terms[item_id] = search_term
                    received_ids.add(item_id)

            # Track items that didn't get search terms
            for item_id in batch_ids:
                if item_id not in received_ids:
                    failed_items.append(item_id)

        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse JSON for batch {batch_num}: {e}")
            failed_items.extend(batch_ids)
            continue

    return all_terms, failed_items


def write_temp_csv(
    items: list[OfferPart],
    search_terms: dict[int, str],
    path: Path,
    weights: dict[int, float | None] | None = None,
) -> None:
    """Write items to CSV format expected by scraper."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "id", "category_id", "db_name", "search_term", "our_price", "our_weight_g", "our_per_qty"
        ])
        writer.writeheader()

        for item in items:
            search_term = search_terms.get(item.id, item.name)
            weight = weights.get(item.id) if weights else None
            writer.writerow({
                "id": item.id,
                "category_id": item.category_id,
                "db_name": item.name,
                "search_term": search_term,
                "our_price": item.price,
                "our_weight_g": weight if weight is not None else "",
                "our_per_qty": "",
            })


def run_scraper(input_csv: Path, output_dir: Path, headless: bool = False) -> Path | None:
    """Run the scraper and return path to results JSON."""
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")

    cmd = [
        sys.executable, "-m", "scraper.main",
        "--input", str(input_csv),
        "--output", str(output_dir),
        "--run-id", f"pipeline_{timestamp}",
    ]

    if headless:
        cmd.append("--headless")

    logger.info(f"Running scraper: {' '.join(cmd)}")

    timeout_seconds = 1800  # 30 min timeout
    process = None

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            logger.warning("Scraper timeout - attempting graceful shutdown...")
            # Try graceful termination first (SIGTERM on Unix, terminate on Windows)
            process.terminate()
            try:
                # Wait up to 30 seconds for graceful shutdown
                stdout, stderr = process.communicate(timeout=30)
                logger.info("Scraper terminated gracefully after timeout")
            except subprocess.TimeoutExpired:
                # Force kill if graceful shutdown fails
                logger.warning("Graceful shutdown failed - force killing scraper")
                process.kill()
                stdout, stderr = process.communicate()
            logger.error("Scraper timed out")
            return None

        if process.returncode != 0:
            logger.error(f"Scraper failed: {stderr}")
            return None

        # Find the output file
        results_file = output_dir / f"results_pipeline_{timestamp}.json"
        if results_file.exists():
            return results_file

        # Try to find any matching file
        for f in output_dir.glob(f"results_pipeline_{timestamp}*.json"):
            return f

        logger.error("Could not find scraper output file")
        return None

    except Exception as e:
        logger.error(f"Scraper failed: {e}")
        if process is not None:
            process.kill()
        return None


def run_processor(input_csv: Path, results_json: Path, output_csv: Path) -> bool:
    """Run the processor and return True on success."""
    cmd = [
        sys.executable, "-m", "scraper.processor",
        "--input", str(input_csv),
        "--results", str(results_json),
        "--output", str(output_csv),
    ]

    logger.info(f"Running processor: {' '.join(cmd)}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)  # 10 min timeout

        if result.returncode != 0:
            logger.error(f"Processor failed: {result.stderr}")
            return False

        return output_csv.exists()

    except subprocess.TimeoutExpired:
        logger.error("Processor timed out")
        return False
    except Exception as e:
        logger.error(f"Processor failed: {e}")
        return False


def parse_comparison_csv(csv_path: Path) -> list[ComparisonResult]:
    """Parse the processor output CSV."""
    results = []
    parse_errors = 0

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row_num, row in enumerate(reader, start=2):  # Start at 2 (header is row 1)
            store_a_cents = None
            if row.get(f"{STORE_A_COL}_converted_cents"):
                try:
                    store_a_cents = int(row[f"{STORE_A_COL}_converted_cents"])
                except ValueError as e:
                    logger.warning(f"Row {row_num}: Invalid {STORE_A_COL}_converted_cents '{row.get(f'{STORE_A_COL}_converted_cents')}': {e}")
                    parse_errors += 1

            store_b_cents = None
            if row.get(f"{STORE_B_COL}_converted_cents"):
                try:
                    store_b_cents = int(row[f"{STORE_B_COL}_converted_cents"])
                except ValueError as e:
                    logger.warning(f"Row {row_num}: Invalid {STORE_B_COL}_converted_cents '{row.get(f'{STORE_B_COL}_converted_cents')}': {e}")
                    parse_errors += 1

            store_a_multiplier = 1.0
            if row.get(f"{STORE_A_COL}_qty_multiplier"):
                try:
                    store_a_multiplier = float(row[f"{STORE_A_COL}_qty_multiplier"])
                except ValueError as e:
                    logger.warning(f"Row {row_num}: Invalid {STORE_A_COL}_qty_multiplier: {e}")

            store_b_multiplier = 1.0
            if row.get(f"{STORE_B_COL}_qty_multiplier"):
                try:
                    store_b_multiplier = float(row[f"{STORE_B_COL}_qty_multiplier"])
                except ValueError as e:
                    logger.warning(f"Row {row_num}: Invalid {STORE_B_COL}_qty_multiplier: {e}")

            try:
                results.append(ComparisonResult(
                    id=int(row["id"]),
                    db_name=row["db_name"],
                    our_price_cents=int(row["our_price_cents"]),
                    store_a_converted_cents=store_a_cents,
                    store_a_match_quality=row.get(f"{STORE_A_COL}_match_quality", "none"),
                    store_a_qty_multiplier=store_a_multiplier,
                    store_b_converted_cents=store_b_cents,
                    store_b_match_quality=row.get(f"{STORE_B_COL}_match_quality", "none"),
                    store_b_qty_multiplier=store_b_multiplier,
                ))
            except (ValueError, KeyError) as e:
                logger.warning(f"Row {row_num}: Failed to parse row: {e}")
                parse_errors += 1

    if parse_errors > 0:
        logger.warning(f"Total parsing errors: {parse_errors}")

    return results


def calculate_rrp(
    store_a_cents: int | None,
    store_b_cents: int | None,
    store_a_qty_multiplier: float = 1.0,
    store_b_qty_multiplier: float = 1.0,
    store_a_quality: str = "none",
    store_b_quality: str = "none",
    quality_ranking: dict | None = None,
) -> int | None:
    """
    Calculate recommended price from competitor data using configured strategy.

    Quality-aware selection:
    - If both stores have the same quality, use max price (standard RRP logic)
    - If qualities differ, use only the better quality result
    - "none" quality means no valid match (excluded)
    """
    if quality_ranking is None:
        quality_ranking = SettingsManager().load().quality_ranking

    adjusted_store_a = None
    if store_a_cents is not None and store_a_quality != "none":
        adjusted_store_a = round(store_a_cents * store_a_qty_multiplier)

    adjusted_store_b = None
    if store_b_cents is not None and store_b_quality != "none":
        adjusted_store_b = round(store_b_cents * store_b_qty_multiplier)

    if adjusted_store_a is None and adjusted_store_b is None:
        return None
    if adjusted_store_a is None:
        return adjusted_store_b
    if adjusted_store_b is None:
        return adjusted_store_a

    store_a_rank = quality_ranking.get(store_a_quality, 0)
    store_b_rank = quality_ranking.get(store_b_quality, 0)

    if store_a_rank == store_b_rank:
        return max(adjusted_store_a, adjusted_store_b)
    elif store_a_rank > store_b_rank:
        return adjusted_store_a
    else:
        return adjusted_store_b


def best_quality(
    store_a_quality: str,
    store_b_quality: str,
    quality_ranking: dict | None = None,
) -> Literal["good", "ok", "poor"]:
    """Get the best quality between two stores."""
    if quality_ranking is None:
        quality_ranking = SettingsManager().load().quality_ranking
    store_a_rank = quality_ranking.get(store_a_quality, 0)
    store_b_rank = quality_ranking.get(store_b_quality, 0)

    best_rank = max(store_a_rank, store_b_rank)

    if best_rank == 3:
        return "good"
    elif best_rank == 2:
        return "ok"
    else:
        return "poor"


def build_updates(comparisons: list[ComparisonResult]) -> list[UpdateRow]:
    """Build update rows from comparison results."""
    settings = SettingsManager().load()
    updates = []

    for row in comparisons:
        rrp = calculate_rrp(
            row.store_a_converted_cents, row.store_b_converted_cents,
            store_a_qty_multiplier=row.store_a_qty_multiplier,
            store_b_qty_multiplier=row.store_b_qty_multiplier,
            store_a_quality=row.store_a_match_quality,
            store_b_quality=row.store_b_match_quality,
            quality_ranking=settings.quality_ranking,
        )

        if rrp is None:
            continue

        quality = best_quality(
            row.store_a_match_quality, row.store_b_match_quality,
            quality_ranking=settings.quality_ranking,
        )

        # Use multiplier-adjusted prices for display
        adjusted_a = round(row.store_a_converted_cents * row.store_a_qty_multiplier) \
            if row.store_a_converted_cents is not None else None
        adjusted_b = round(row.store_b_converted_cents * row.store_b_qty_multiplier) \
            if row.store_b_converted_cents is not None else None

        updates.append(UpdateRow(
            id=row.id,
            name=row.db_name,
            current_price=row.our_price_cents,
            new_rrp=rrp,
            store_a_price=adjusted_a,
            store_b_price=adjusted_b,
            quality=quality,
            selected=(quality in settings.auto_approve_qualities),
        ))

    return updates


def write_audit_log(updates: list[UpdateRow], log_path: Path) -> None:
    """Write audit log of changes."""
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"Pipeline run: {datetime.now().isoformat()}\n")
        f.write("=" * 60 + "\n\n")

        for update in updates:
            if update.selected:
                f.write(f"ID: {update.id}\n")
                f.write(f"Name: {update.name}\n")
                f.write(f"Our price: {update.current_price} cents\n")
                f.write(f"New RRP: {update.new_rrp} cents\n")
                f.write(f"{STORE_A_NAME}: {update.store_a_price} cents\n")
                f.write(f"{STORE_B_NAME}: {update.store_b_price} cents\n")
                f.write(f"Quality: {update.quality}\n")
                if update.rrp_source:
                    f.write(f"RRP source: {update.rrp_source} (MANUAL OVERRIDE)\n")
                f.write("-" * 40 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Automated Price Comparison Pipeline")
    parser.add_argument("--offer-id", type=int, required=True, help="The offer_id to process")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change, don't update DB")
    parser.add_argument("--fully-automated", action="store_true", help="Skip TUI approval, auto-accept good/ok matches")
    parser.add_argument("--skip-scrape", action="store_true", help="Use existing results JSON (for re-processing)")
    parser.add_argument("--results", type=Path, help="Path to existing results JSON")
    parser.add_argument("--limit", type=int, help="Process only first N items (for testing)")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--output-dir", type=Path, default=Path("output"), help="Output directory (default: output)")

    args = parser.parse_args()

    offer_id = args.offer_id
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Verify offer exists and fetch items
    logger.info(f"Checking offer_id={offer_id}...")

    if not verify_offer_exists(offer_id):
        logger.error(f"No F&V items found for offer_id={offer_id}")
        sys.exit(1)

    logger.info("Fetching items from database...")
    items = fetch_items(offer_id, limit=args.limit)

    if not items:
        logger.error("No items to process")
        sys.exit(1)

    logger.info(f"Found {len(items)} items to process")

    # Step 2: Generate search terms
    logger.info("Generating search terms with Claude...")
    items_for_terms = [{"id": item.id, "name": item.name} for item in items]
    search_terms, failed_search_term_ids = generate_search_terms_batch(items_for_terms, batch_size=15)
    logger.info(f"Generated search terms for {len(search_terms)} items")

    if failed_search_term_ids:
        logger.warning(f"Failed to generate search terms for {len(failed_search_term_ids)} items: {failed_search_term_ids}")

    # Write temp CSV for scraper
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    temp_csv = output_dir / f"pipeline_input_{timestamp}.csv"
    write_temp_csv(items, search_terms, temp_csv)
    logger.info(f"Wrote input CSV: {temp_csv}")

    # Step 3: Run scraper (or use existing results)
    if args.skip_scrape:
        if not args.results:
            logger.error("--skip-scrape requires --results path")
            sys.exit(1)
        results_json = args.results
        if not results_json.exists():
            logger.error(f"Results file not found: {results_json}")
            sys.exit(1)
    else:
        logger.info("Running scraper...")
        results_json = run_scraper(temp_csv, output_dir, headless=args.headless)
        if not results_json:
            logger.error("Scraper failed")
            sys.exit(1)
        logger.info(f"Scraper complete: {results_json}")

    # Step 4: Run processor
    comparison_csv = output_dir / f"pipeline_comparison_{timestamp}.csv"
    logger.info("Running processor...")
    if not run_processor(temp_csv, results_json, comparison_csv):
        logger.error("Processor failed")
        sys.exit(1)
    logger.info(f"Processor complete: {comparison_csv}")

    # Step 5: Build updates
    logger.info("Building update list...")
    comparisons = parse_comparison_csv(comparison_csv)
    updates = build_updates(comparisons)
    logger.info(f"Found {len(updates)} items with valid competitor prices")

    if not updates:
        logger.info("No updates to apply")
        sys.exit(0)

    # Step 6: TUI approval or auto-accept
    if args.fully_automated:
        logger.info("Fully automated mode: accepting good/ok matches")
        approved = [u for u in updates if u.selected]
    else:
        logger.info("Showing approval TUI...")
        approved = show_approval_tui(updates)

        if approved is None:
            logger.info("Cancelled by user")
            sys.exit(0)

    if not approved:
        logger.info("No items selected for update")
        sys.exit(0)

    logger.info(f"Approved {len(approved)} items for update")

    # Write audit log
    audit_log = output_dir / f"pipeline_audit_{timestamp}.log"
    write_audit_log(approved, audit_log)
    logger.info(f"Wrote audit log: {audit_log}")

    # Step 7: Execute updates
    update_tuples = [(u.new_rrp, u.id) for u in approved]

    if args.dry_run:
        logger.info("[DRY RUN] Would update the following items:")
        for u in approved:
            logger.info(f"  ID {u.id}: RRP = {u.new_rrp} cents ({u.quality})")
        execute_updates(update_tuples, dry_run=True)
    else:
        logger.info("Executing database updates...")
        affected = execute_updates(update_tuples, dry_run=False)
        logger.info(f"Updated {affected} rows in database")

    # Show summary
    show_summary(updates, executed=not args.dry_run)

    # Generate HTML report
    try:
        from scraper.html_report import generate_html_report
        html_path = generate_html_report(
            updates=updates,
            approved=approved,
            comparison_csv_path=comparison_csv,
            offer_id=offer_id,
            timestamp=timestamp,
            output_dir=output_dir,
        )
        if html_path:
            logger.info(f"HTML report: {html_path}")
    except Exception as e:
        logger.warning(f"Could not generate HTML report: {e}")

    print(f"\n{'=' * 50}")
    print("PIPELINE COMPLETE")
    print(f"{'=' * 50}")
    print(f"Items processed: {len(items)}")
    print(f"Search terms generated: {len(search_terms)}")
    if failed_search_term_ids:
        print(f"Search term failures: {len(failed_search_term_ids)}")
    print(f"Items with prices: {len(updates)}")
    print(f"Items updated: {len(approved)}")
    print(f"Output directory: {output_dir}")


if __name__ == "__main__":
    main()
