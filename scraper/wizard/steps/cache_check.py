"""Step 2: Check for cached data from previous runs."""

import logging
import re
from datetime import datetime
from pathlib import Path

from scraper.wizard.components.help_content import HelpEntry, HelpTip
from scraper.wizard.components.menu import Menu, MenuItem
from scraper.wizard.state import CacheInfo, WizardState

logger = logging.getLogger(__name__)


# Help content for cache options
CACHE_BOTH_HELP = HelpEntry([
    HelpTip(
        "Skip both the search term generation and web scraping steps entirely. "
        "This reuses the price data from your last run and only re-processes "
        "the matches. Fastest option, ideal for reviewing previous results."
    ),
])

CACHE_CSV_HELP = HelpEntry([
    HelpTip(
        "Reuse the search terms from your last run but scrape fresh prices from "
        "competitor stores. Use this when you want updated prices but don't "
        "need to regenerate search terms."
    ),
])

FRESH_HELP = HelpEntry([
    HelpTip(
        "Start completely fresh: generate new search terms using the LLM and "
        "scrape all prices from scratch. Takes the longest but ensures everything "
        "is up to date. Use this after database changes."
    ),
])


def find_cached_files(output_dir: Path, offer_id: int) -> CacheInfo:
    """
    Find cached CSV and results files for an offer.

    Looks for files with offer_id in filename:
    - pipeline_input_{offer_id}_*.csv
    - results_pipeline_{offer_id}_*.json

    Falls back to legacy files without offer_id for backwards compatibility.
    """
    cache = CacheInfo()

    if not output_dir.exists():
        return cache

    # First try to find files with offer_id in filename (new format)
    csv_files = sorted(
        output_dir.glob(f"pipeline_input_{offer_id}_*.csv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    results_files = sorted(
        output_dir.glob(f"results_pipeline_{offer_id}_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    # Fall back to legacy format (without offer_id) if no new format files found
    if not csv_files:
        csv_files = sorted(
            output_dir.glob("pipeline_input_*.csv"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        # Filter out files that have an offer_id in them (they belong to other offers)
        csv_files = [f for f in csv_files if not re.match(r"pipeline_input_\d+_", f.name)]

    if not results_files:
        results_files = sorted(
            output_dir.glob("results_pipeline_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        # Filter out files that have an offer_id in them (they belong to other offers)
        results_files = [f for f in results_files if not re.match(r"results_pipeline_\d+_", f.name)]

    # Find most recent files
    if csv_files:
        cache.csv_path = csv_files[0]
        cache.timestamp = datetime.fromtimestamp(csv_files[0].stat().st_mtime)

    if results_files:
        cache.results_path = results_files[0]
        if cache.timestamp is None:
            cache.timestamp = datetime.fromtimestamp(results_files[0].stat().st_mtime)

    return cache


def run_cache_check(state: WizardState, output_dir: Path) -> bool:
    """
    Check for cached data and ask user how to proceed.

    Returns:
        True to continue, False if cancelled.
    """
    cache = find_cached_files(output_dir, state.offer_id)
    state.cache = cache

    # No cache found - proceed automatically
    if cache.csv_path is None and cache.results_path is None:
        logger.info("No cached data found, will run fresh")
        state.use_cached_csv = False
        state.use_cached_results = False
        return True

    # Build options based on what's available
    items: list[MenuItem[str]] = []

    if cache.csv_path and cache.results_path:
        timestamp_str = cache.timestamp.strftime("%Y-%m-%d %H:%M") if cache.timestamp else "unknown"
        items.append(
            MenuItem(
                label="Use previous run entirely",
                description=f"Skip scraping, just re-process matches from last run ({timestamp_str})",
                value="both",
                badge="Fastest",
                help=CACHE_BOTH_HELP,
            )
        )
        items.append(
            MenuItem(
                label="Re-scrape only",
                description="Keep existing search terms but scrape fresh prices from stores",
                value="csv_only",
                help=CACHE_CSV_HELP,
            )
        )

    elif cache.csv_path:
        timestamp_str = cache.timestamp.strftime("%Y-%m-%d %H:%M") if cache.timestamp else "unknown"
        items.append(
            MenuItem(
                label="Re-scrape only",
                description=f"Keep existing search terms but scrape fresh prices ({timestamp_str})",
                value="csv_only",
                help=CACHE_CSV_HELP,
            )
        )

    items.append(
        MenuItem(
            label="Start fresh",
            description="Generate new search terms and scrape everything from scratch",
            value="fresh",
            help=FRESH_HELP,
        )
    )

    menu = Menu(title="Cached Data Found", items=items)

    result = menu.show()

    if result is None:
        return False

    if result == "both":
        state.use_cached_csv = True
        state.use_cached_results = True
        state.temp_csv_path = cache.csv_path
        state.results_path = cache.results_path
        logger.info(f"Using cached CSV: {cache.csv_path}")
        logger.info(f"Using cached results: {cache.results_path}")
    elif result == "csv_only":
        state.use_cached_csv = True
        state.use_cached_results = False
        state.temp_csv_path = cache.csv_path
        logger.info(f"Using cached CSV: {cache.csv_path}")
    else:
        state.use_cached_csv = False
        state.use_cached_results = False
        logger.info("Starting fresh run")

    return True
