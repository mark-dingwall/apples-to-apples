import argparse
import asyncio
import csv
import json
import logging
import sys
import threading
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable

from scraper.models import (
    InputItem,
    ItemResults,
    RunSummary,
    ScrapeRun,
    StoreResults,
)
from scraper.stores.store_a import StoreAScraper, STORE_CONFIG as STORE_A_CONFIG
from scraper.stores.store_b import StoreBScraper, STORE_CONFIG as STORE_B_CONFIG
from scraper.stores.store_config import STORE_A_COL, STORE_B_COL
from scraper.utils.matching import extract_search_term
from scraper.utils.stealth import create_stealth_browser

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def load_items(csv_path: Path) -> list[InputItem]:
    """Load items from CSV file."""
    from scraper.wizard.settings import SettingsManager
    fallback = SettingsManager().load().category_id_fallback
    items = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row_num, row in enumerate(reader, start=2):
            try:
                # Support both standalone (name/price) and pipeline (db_name/our_price) CSV formats
                name = (row.get("name") or row.get("db_name", "")).strip()
                price_str = row.get("price") or row.get("our_price", "")

                if not name or not price_str:
                    logger.warning(f"Row {row_num}: Missing name or price, skipping")
                    continue

                # Use pre-generated search_term if available, otherwise extract from name
                search_term = row.get("search_term", "").strip()
                if not search_term:
                    search_term = extract_search_term(name)

                cat_id_val = row.get("category_id") or (row.get(fallback, 0) if fallback else 0)
                items.append(InputItem(
                    id=int(row["id"]),
                    category_id=int(cat_id_val),
                    name=name,
                    price_cents=int(price_str),
                    extracted_search_term=search_term,
                ))
            except (ValueError, KeyError) as e:
                logger.warning(f"Row {row_num}: Invalid data ({e}), skipping")

    return items


async def scrape_store(
    scraper,
    items: list[InputItem],
    on_item_done: Callable[[], None] | None = None,
) -> dict[int, StoreResults]:
    """Scrape all items from a single store."""
    results = {}

    await scraper.init_page()

    try:
        for item in items:
            result = await scraper.search(item.extracted_search_term)
            results[item.id] = result
            logger.info(
                f"[{scraper.name}] {item.extracted_search_term}: "
                f"{result.status} ({len(result.results)} results)"
            )
            if on_item_done:
                on_item_done()
    finally:
        await scraper.close_page()

    return results


def _get_screen_size() -> tuple[int, int]:
    """Get screen resolution. Falls back to 1920x1080."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    except Exception:
        return 1920, 1080


async def run_scraper(
    items: list[InputItem],
    headless: bool = False,
    stores: str = "all",
    debug_mouse: bool = False,
    proxy: str | None = None,
    progress_file: Path | None = None,
) -> list[ItemResults]:
    """Run scrapers for specified stores (in parallel when both are enabled)."""
    run_store_a = stores in ("all", "store_a")
    run_store_b = stores in ("all", "store_b")

    # Progress tracking: total = items * number_of_stores
    num_stores = (1 if run_store_a else 0) + (1 if run_store_b else 0)
    progress_total = len(items) * num_stores
    progress_completed = 0
    progress_lock = threading.Lock()

    def _on_item_done() -> None:
        nonlocal progress_completed
        with progress_lock:
            progress_completed += 1
            if progress_file:
                try:
                    progress_file.write_text(
                        json.dumps({"completed": progress_completed, "total": progress_total}),
                        encoding="utf-8",
                    )
                except Exception:
                    pass

    # Write initial progress
    if progress_file:
        try:
            progress_file.write_text(
                json.dumps({"completed": 0, "total": progress_total}),
                encoding="utf-8",
            )
        except Exception:
            pass

    # Calculate window bounds for stacked browser windows
    store_a_bounds = None
    store_b_bounds = None
    if run_store_a and run_store_b and not headless:
        screen_w, screen_h = _get_screen_size()
        usable_h = screen_h - 48  # subtract taskbar
        half_h = usable_h // 2
        store_a_bounds = (0, 0, screen_w, half_h)
        store_b_bounds = (0, half_h, screen_w, half_h)

    # Initialize empty results for each item (will be overwritten by actual results)
    item_results_map: dict[int, ItemResults] = {}
    for item in items:
        item_results_map[item.id] = ItemResults(
            input=item,
            store_a=StoreResults(status="skipped"),
            store_b=StoreResults(status="skipped"),
        )

    async def scrape_store_a() -> dict[int, StoreResults] | Exception:
        """Scrape Store A with its own browser instance."""
        logger.info(f"Starting {STORE_A_CONFIG['display_name']} scraper...")
        playwright, browser, context = await create_stealth_browser(
            headless=headless, proxy=proxy, window_bounds=store_a_bounds,
            locale=STORE_A_CONFIG["locale"], timezone_id=STORE_A_CONFIG["timezone"],
        )
        try:
            scraper = StoreAScraper(context, debug_mouse=debug_mouse)
            return await scrape_store(scraper, items, on_item_done=_on_item_done)
        finally:
            await context.close()
            await browser.close()
            await playwright.stop()

    async def scrape_store_b() -> dict[int, StoreResults] | Exception:
        """Scrape Store B with its own browser instance."""
        logger.info(f"Starting {STORE_B_CONFIG['display_name']} scraper...")
        playwright, browser, context = await create_stealth_browser(
            headless=headless, proxy=proxy, window_bounds=store_b_bounds,
            locale=STORE_B_CONFIG["locale"], timezone_id=STORE_B_CONFIG["timezone"],
        )
        try:
            scraper = StoreBScraper(context, debug_mouse=debug_mouse)
            return await scrape_store(scraper, items, on_item_done=_on_item_done)
        finally:
            await context.close()
            await browser.close()
            await playwright.stop()

    # Run stores in parallel (separate browser instances, no cross-detection risk)
    if run_store_a and run_store_b:
        results = await asyncio.gather(
            scrape_store_a(),
            scrape_store_b(),
            return_exceptions=True,
        )
        store_a_results, store_b_results = results

        # Handle Store A results or exception
        if isinstance(store_a_results, Exception):
            logger.error(f"{STORE_A_CONFIG['display_name']} scraper failed: {store_a_results}")
        else:
            for item_id, result in store_a_results.items():
                item_results_map[item_id].store_a = result

        # Handle Store B results or exception
        if isinstance(store_b_results, Exception):
            logger.error(f"{STORE_B_CONFIG['display_name']} scraper failed: {store_b_results}")
        else:
            for item_id, result in store_b_results.items():
                item_results_map[item_id].store_b = result
    elif run_store_a:
        try:
            store_a_results = await scrape_store_a()
            if not isinstance(store_a_results, Exception):
                for item_id, result in store_a_results.items():
                    item_results_map[item_id].store_a = result
        except Exception as e:
            logger.error(f"{STORE_A_CONFIG['display_name']} scraper failed: {e}")
    elif run_store_b:
        try:
            store_b_results = await scrape_store_b()
            if not isinstance(store_b_results, Exception):
                for item_id, result in store_b_results.items():
                    item_results_map[item_id].store_b = result
        except Exception as e:
            logger.error(f"{STORE_B_CONFIG['display_name']} scraper failed: {e}")

    return list(item_results_map.values())


def calculate_summary(item_results: list[ItemResults]) -> RunSummary:
    """Calculate summary statistics."""
    store_a_success = sum(1 for ir in item_results if ir.store_a.status == "success")
    store_b_success = sum(1 for ir in item_results if ir.store_b.status == "success")

    return RunSummary(
        total_items=len(item_results),
        store_a_success=store_a_success,
        store_b_success=store_b_success,
    )


def save_results(run: ScrapeRun, output_path: Path) -> None:
    """Save results to JSON file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        data = asdict(run)
        # Rename generic store_a/store_b keys to real store names in output JSON
        for item in data["items"]:
            item[STORE_A_COL] = item.pop("store_a")
            item[STORE_B_COL] = item.pop("store_b")
        summary = data["summary"]
        summary[f"{STORE_A_COL}_success"] = summary.pop("store_a_success")
        summary[f"{STORE_B_COL}_success"] = summary.pop("store_b_success")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        logger.error(f"Failed to serialize results: {e}")
        raise
    except IOError as e:
        logger.error(f"Failed to write results file: {e}")
        raise

    logger.info(f"Results saved to: {output_path}")


async def main():
    parser = argparse.ArgumentParser(description="Fruit & Veg Price Scraper")
    parser.add_argument("--input", required=True, help="Path to input CSV file")
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--run-id", help="Custom run identifier for output filename")
    parser.add_argument(
        "--store",
        choices=["store_a", "store_b", "all"],
        default="all",
        help="Which store(s) to scrape (default: all)"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit to first N items (0 = no limit, for testing use --limit 1)"
    )
    parser.add_argument(
        "--interactive-oos-check",
        action="store_true",
        help="Hunt for OOS products with interactive confirmation (requires --store store_a or store_b)"
    )
    parser.add_argument(
        "--debug-mouse",
        action="store_true",
        help="Show red cursor indicator to visualize mouse movements (useful for debugging)"
    )
    parser.add_argument(
        "--proxy",
        type=str,
        help="Proxy server URL (e.g., http://proxy:8080 or socks5://proxy:1080)"
    )
    parser.add_argument(
        "--progress-file",
        type=str,
        help="Path to write progress updates (JSON with completed/total)"
    )

    args = parser.parse_args()

    # Validate run-id to prevent path traversal
    if args.run_id:
        if "/" in args.run_id or "\\" in args.run_id or ".." in args.run_id:
            logger.error("Invalid --run-id: must not contain path separators or '..'")
            sys.exit(1)

    # Handle interactive OOS mode
    if args.interactive_oos_check:
        if args.store == "all":
            logger.error("--interactive-oos-check requires --store store_a or --store store_b (not 'all')")
            sys.exit(1)

        from scraper.oos_mode import run_oos_detection, load_items as oos_load_items

        input_path = Path(args.input)
        if not input_path.exists():
            logger.error(f"Input file not found: {input_path}")
            sys.exit(1)

        items = oos_load_items(input_path)
        if not items:
            logger.error("No valid items found in input file")
            sys.exit(1)

        if args.limit > 0:
            items = items[:args.limit]

        await run_oos_detection(args.store, items, Path(".ref"))
        return

    input_path = Path(args.input)
    output_dir = Path(args.output)

    if not input_path.exists():
        logger.error(f"Input file not found: {input_path}")
        sys.exit(1)

    # Load items
    logger.info(f"Loading items from: {input_path}")
    items = load_items(input_path)
    logger.info(f"Loaded {len(items)} items")

    if not items:
        logger.error("No valid items found in input file")
        sys.exit(1)

    # Apply limit if specified
    if args.limit > 0:
        items = items[:args.limit]
        logger.info(f"Limited to {len(items)} item(s) for testing")

    # Run scraper
    timestamp = datetime.now()
    pf = Path(args.progress_file) if args.progress_file else None
    item_results = await run_scraper(
        items,
        headless=args.headless,
        stores=args.store,
        debug_mouse=args.debug_mouse,
        proxy=args.proxy,
        progress_file=pf,
    )

    # Calculate summary
    summary = calculate_summary(item_results)

    # Build run object
    run = ScrapeRun(
        run_timestamp=timestamp.isoformat(),
        items=item_results,
        summary=summary,
    )

    # Generate output filename
    if args.run_id:
        filename = f"results_{args.run_id}.json"
    else:
        filename = f"results_{timestamp.strftime('%Y-%m-%d_%H%M%S')}.json"

    output_path = output_dir / filename

    # Save results
    save_results(run, output_path)

    # Print summary
    print(f"\n{'='*50}")
    print("SCRAPE COMPLETE")
    print(f"{'='*50}")
    print(f"Total items: {summary.total_items}")
    print(f"{STORE_A_CONFIG['display_name']} success: {summary.store_a_success}")
    print(f"{STORE_B_CONFIG['display_name']} success: {summary.store_b_success}")
    print(f"Output: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
