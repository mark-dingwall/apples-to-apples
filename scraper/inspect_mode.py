"""
Interactive inspect mode for debugging selectors.

Opens a browser to a store's search results and pauses for manual inspection.
Use DevTools (F12) to find correct CSS selectors, then press Enter to save HTML.

Usage:
    python -m scraper.inspect_mode --store store_b --search "apples"
    python -m scraper.inspect_mode --store store_a --search "bananas"
"""

import argparse
import asyncio
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

from scraper.utils.stealth import create_stealth_browser, apply_stealth

try:
    from scraper.stores.store_config import STORE_A, STORE_B
except ImportError:
    raise ImportError(
        "Store config not found. Copy scraper/stores/store_config.example.py "
        "to scraper/stores/store_config.py and fill in your store details."
    )


async def run_inspect(store: str, search_term: str, output_dir: Path) -> None:
    """Open browser to search results and wait for user inspection."""

    # Get the search URL for the store
    cfg = STORE_A if store == "store_a" else STORE_B
    url = cfg["search_url"].format(query=quote_plus(search_term))

    print(f"Launching browser for {store}...")
    print(f"Search URL: {url}")

    playwright, browser, context = await create_stealth_browser(headless=False)

    try:
        page = await context.new_page()
        await apply_stealth(page)

        print("Navigating to search results...")
        await page.goto(url, wait_until="domcontentloaded")

        # Wait a bit for dynamic content
        await asyncio.sleep(3)

        print("\n" + "=" * 60)
        print("BROWSER READY FOR INSPECTION")
        print("=" * 60)
        print("\nInstructions:")
        print("1. Press F12 to open DevTools")
        print("2. Use the element picker (Ctrl+Shift+C) to inspect product tiles")
        print("3. Look for CSS selectors that identify:")
        print("   - Product tile container")
        print("   - Product name")
        print("   - Price")
        print("   - Unit price (e.g., '$5.90 per kg')")
        print("   - Product link")
        print("4. Take screenshots if helpful")
        print("5. Press ENTER here when done to save page HTML")
        print("=" * 60 + "\n")

        # Wait for user
        input("Press ENTER when done inspecting...")

        # Save the HTML
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        html_file = output_dir / f"inspect_{store}_{timestamp}.html"

        try:
            html_content = await page.content()
            html_file.write_text(html_content, encoding="utf-8")
        except IOError as e:
            print(f"Error saving HTML: {e}")
            raise

        print(f"\nPage HTML saved to: {html_file}")

    finally:
        await context.close()
        await browser.close()
        await playwright.stop()


def main():
    parser = argparse.ArgumentParser(
        description="Interactive inspect mode for debugging store selectors"
    )
    parser.add_argument(
        "--store",
        required=True,
        choices=["store_a", "store_b"],
        help="Store to inspect"
    )
    parser.add_argument(
        "--search",
        default="apples",
        help="Search term to use (default: apples)"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output"),
        help="Output directory for HTML files (default: output)"
    )

    args = parser.parse_args()

    asyncio.run(run_inspect(args.store, args.search, args.output))


if __name__ == "__main__":
    main()
