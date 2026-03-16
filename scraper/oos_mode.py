"""
Interactive OOS (out-of-stock) detection mode.

Hunts for out-of-stock products during scrapes, pauses for user confirmation,
and captures verified samples for building automated detection.

Usage:
    python -m scraper.oos_mode --store store_a --input input/items.csv
    python -m scraper.oos_mode --store store_b --input input/items.csv --limit 20
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

from scraper import config
from scraper.main import load_items
from scraper.models import InputItem
from scraper.utils.oos_detection import FalsePositiveTracker, OOSHeuristics
from scraper.utils.stealth import apply_stealth, create_stealth_browser

try:
    from scraper.stores.store_config import STORE_A, STORE_B
except ImportError:
    raise ImportError(
        "Store config not found. Copy scraper/stores/store_config.example.py "
        "to scraper/stores/store_config.py and fill in your store details."
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Known in-stock baseline product
BASELINE_SEARCH_TERM = "Pink Lady apples"


def get_search_url(store: str, query: str) -> str:
    """Get search URL for a store."""
    cfg = STORE_A if store == "store_a" else STORE_B
    return cfg["search_url"].format(query=quote_plus(query))


def get_tile_selector(store: str) -> str:
    """Get product tile selector for a store."""
    cfg = STORE_A if store == "store_a" else STORE_B
    return cfg["selectors"]["product_tile"]


def get_name_selector(store: str) -> str:
    """Get product name selector for a store."""
    cfg = STORE_A if store == "store_a" else STORE_B
    return cfg["selectors"]["product_name"]


async def capture_baseline(page, store: str, heuristics: OOSHeuristics, baseline_term: str) -> bool:
    """Capture baseline from known in-stock product."""
    print(f"\n[INFO] Capturing baseline from \"{baseline_term}\"...")

    url = get_search_url(store, baseline_term)
    await page.goto(url, wait_until="domcontentloaded")
    await asyncio.sleep(3)  # Wait for dynamic content

    tile_selector = get_tile_selector(store)
    tiles = page.locator(tile_selector)
    count = await tiles.count()

    if count == 0:
        print("[WARN] No baseline products found - continuing without baseline")
        return False

    # Get first tile's HTML as baseline
    first_tile = tiles.first
    baseline_html = await first_tile.evaluate("el => el.outerHTML")
    heuristics.set_baseline(baseline_html)

    print(f"[INFO] Baseline captured: {count} in-stock products found")
    return True


async def prompt_user(product_name: str, triggers, heuristics: OOSHeuristics) -> str:
    """
    Prompt user for confirmation.
    Returns: 'y' (yes), 'n' (no), or 'q' (quit)
    """
    print("\n" + "=" * 60)
    print("[ALERT] Potential OOS detected!")
    print("=" * 60)
    print(f"\n  Product: \"{product_name}\"")
    print(f"\n  Triggers:")
    print(heuristics.format_triggers(triggers))
    print()

    while True:
        response = input("  Is this a genuine OOS item? [y/n/q]: ").strip().lower()
        if response in ("y", "n", "q", "yes", "no", "quit"):
            return response[0]
        print("  Please enter 'y' (yes), 'n' (no), or 'q' (quit)")


async def save_oos_sample(
    page,
    tile,
    store: str,
    product_name: str,
    triggers,
    output_dir: Path
) -> tuple[Path, Path]:
    """Save OOS sample (both tile HTML and full page)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")

    # Save tile HTML
    tile_html = await tile.evaluate("el => el.outerHTML")
    tile_file = output_dir / f"oos_{store}_tile_{timestamp}.html"
    try:
        tile_file.write_text(tile_html, encoding="utf-8")
    except IOError as e:
        logger.error(f"Failed to write tile HTML: {e}")
        raise

    # Save full page HTML
    page_html = await page.content()
    page_file = output_dir / f"oos_{store}_page_{timestamp}.html"
    try:
        page_file.write_text(page_html, encoding="utf-8")
    except IOError as e:
        logger.error(f"Failed to write page HTML: {e}")
        raise

    return tile_file, page_file


async def run_oos_detection(
    store: str,
    items: list[InputItem],
    output_dir: Path,
    baseline_term: str = BASELINE_SEARCH_TERM,
) -> None:
    """Main OOS detection loop."""

    print("\n" + "=" * 60)
    print(f"[INFO] Starting OOS detection mode for {store}")
    print("=" * 60)

    playwright, browser, context = await create_stealth_browser(headless=False)
    heuristics = OOSHeuristics(store)
    tracker = FalsePositiveTracker()

    try:
        page = await context.new_page()
        await apply_stealth(page)

        # Capture baseline first
        await capture_baseline(page, store, heuristics, baseline_term)

        tile_selector = get_tile_selector(store)
        name_selector = get_name_selector(store)

        # Hunt through items
        for item in items:
            search_term = item.extracted_search_term
            print(f"\n[INFO] Searching: {search_term}")

            url = get_search_url(store, search_term)
            await page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(3)  # Wait for dynamic content

            tiles = page.locator(tile_selector)
            count = await tiles.count()

            if count == 0:
                print(f"[INFO] No results for \"{search_term}\"")
                continue

            # Check each tile for OOS indicators
            for i in range(min(count, config.MAX_RESULTS_PER_STORE)):
                tile = tiles.nth(i)

                # Get product name
                name_locator = tile.locator(name_selector)
                product_name = f"Product {i+1}"
                try:
                    if await name_locator.count() > 0:
                        name_text = await name_locator.first.text_content()
                        if name_text:
                            product_name = name_text.strip()
                except Exception as e:
                    logger.debug(f"[{store}] Error getting product name: {e}")

                # Run heuristics
                triggers = await heuristics.check_tile(tile)

                if not triggers:
                    continue  # No OOS indicators

                # Check if suppressed
                if tracker.is_suppressed(triggers, product_name):
                    print(f"[INFO] Skipping suppressed pattern: {product_name}")
                    continue

                # Prompt user
                response = await prompt_user(product_name, triggers, heuristics)

                if response == "y":
                    # True positive - save and exit
                    tile_file, page_file = await save_oos_sample(
                        page, tile, store, product_name, triggers, output_dir
                    )

                    print("\n" + "=" * 60)
                    print("[SUCCESS] OOS sample captured!")
                    print("=" * 60)
                    print(f"\n  Tile HTML: {tile_file}")
                    print(f"  Full page: {page_file}")
                    print(f"\n  Triggered by:")
                    print(heuristics.format_triggers(triggers))
                    print("\nExiting OOS detection mode.")
                    return

                elif response == "n":
                    # False positive - suppress and continue
                    tracker.suppress(triggers, product_name)
                    print(f"[INFO] Pattern suppressed, continuing...")

                elif response == "q":
                    # Quit without saving
                    print("\n[INFO] Exiting without saving.")
                    return

            # Small delay between searches
            await asyncio.sleep(2)

        print("\n[INFO] Finished searching all items. No confirmed OOS found.")

    finally:
        await context.close()
        await browser.close()
        await playwright.stop()


def main():
    parser = argparse.ArgumentParser(
        description="Interactive OOS detection mode - hunt for out-of-stock products"
    )
    parser.add_argument(
        "--store",
        required=True,
        choices=["store_a", "store_b"],
        help="Store to scan for OOS products"
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Path to input CSV file with items to search"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".ref"),
        help="Output directory for captured HTML (default: .ref)"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit to first N items (0 = no limit)"
    )
    parser.add_argument(
        "--baseline",
        default=BASELINE_SEARCH_TERM,
        help=f"Known in-stock product for baseline comparison (default: {BASELINE_SEARCH_TERM})"
    )

    args = parser.parse_args()

    if not args.input.exists():
        logger.error(f"Input file not found: {args.input}")
        sys.exit(1)

    # Load items
    items = load_items(args.input)
    if not items:
        logger.error("No valid items found in input file")
        sys.exit(1)

    if args.limit > 0:
        items = items[:args.limit]
        print(f"[INFO] Limited to {len(items)} item(s)")

    # Run detection
    asyncio.run(run_oos_detection(args.store, items, args.output, args.baseline))


if __name__ == "__main__":
    main()
