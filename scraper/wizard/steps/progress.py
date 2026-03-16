"""Step 4: Progress display during pipeline execution."""

import csv
import json
import logging
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    BarColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.text import Text

from scraper.db import OfferPart, fetch_items
from scraper.utils.claude_cli import call_claude_cli
from scraper.wizard.state import WizardState

try:
    from scraper.prompts import SEARCH_TERM_WEIGHT_PROMPT
except ImportError:
    raise ImportError(
        "Prompt templates not found. Copy scraper/prompts.example.py "
        "to scraper/prompts.py and customise for your use case."
    )

logger = logging.getLogger(__name__)


def generate_search_terms_with_weights(
    items: list[dict], batch_size: int = 200, progress_callback: Callable[[int, int], None] | None = None
) -> tuple[dict[int, str], dict[int, float | None], dict[int, float | None], list[int]]:
    """
    Generate search terms AND extract weights/per_qty using Claude CLI.

    Returns:
        Tuple of:
        - dict mapping item_id to search_term
        - dict mapping item_id to weight_g (or None)
        - dict mapping item_id to per_qty (or None)
        - list of failed item_ids
    """
    all_terms = {}
    all_weights: dict[int, float | None] = {}
    all_per_qtys: dict[int, float | None] = {}
    failed_items: list[int] = []

    for i in range(0, len(items), batch_size):
        batch = items[i : i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (len(items) + batch_size - 1) // batch_size
        batch_ids = [item["id"] for item in batch]

        if progress_callback:
            progress_callback(i, len(items))

        logger.info(f"Generating search terms batch {batch_num}/{total_batches}...")

        items_json = json.dumps(
            [{"id": item["id"], "name": item["name"]} for item in batch]
        )

        prompt = SEARCH_TERM_WEIGHT_PROMPT.format(items_json=items_json)

        response = call_claude_cli(prompt, model="haiku")

        if not response:
            logger.warning(
                f"Failed to get search terms for batch {batch_num} ({len(batch)} items)"
            )
            failed_items.extend(batch_ids)
            continue

        try:
            json_match = re.search(r"\{[\s\S]*\}", response)
            if not json_match:
                logger.warning(f"No JSON found in response for batch {batch_num}")
                failed_items.extend(batch_ids)
                continue

            data = json.loads(json_match.group())
            terms = data.get("items", [])

            received_ids = set()
            for term_data in terms:
                item_id = term_data.get("id")
                search_term = term_data.get("search_term", "")
                weight_g = term_data.get("weight_g")
                per_qty = term_data.get("per_qty")

                if item_id and search_term:
                    all_terms[item_id] = search_term
                    all_weights[item_id] = weight_g
                    all_per_qtys[item_id] = float(per_qty) if per_qty is not None else None
                    received_ids.add(item_id)

            for item_id in batch_ids:
                if item_id not in received_ids:
                    failed_items.append(item_id)

        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse JSON for batch {batch_num}: {e}")
            failed_items.extend(batch_ids)
            continue

    if progress_callback:
        progress_callback(len(items), len(items))

    return all_terms, all_weights, all_per_qtys, failed_items


def write_pipeline_csv(
    items: list[OfferPart],
    search_terms: dict[int, str],
    weights: dict[int, float | None],
    path: Path,
    per_qtys: dict[int, float | None] | None = None,
) -> None:
    """Write items to CSV format expected by scraper and processor."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "id",
                "category_id",
                "db_name",
                "search_term",
                "our_price",
                "our_weight_g",
                "our_per_qty",
            ],
        )
        writer.writeheader()

        for item in items:
            search_term = search_terms.get(item.id, item.name)
            weight = weights.get(item.id)
            per_qty = per_qtys.get(item.id) if per_qtys else None
            writer.writerow(
                {
                    "id": item.id,
                    "category_id": item.category_id,
                    "db_name": item.name,
                    "search_term": search_term,
                    "our_price": item.price,
                    "our_weight_g": weight if weight is not None else "",
                    "our_per_qty": per_qty if per_qty is not None else "",
                }
            )


def deduplicate_for_scraping(
    items: list[OfferPart], search_terms: dict[int, str]
) -> tuple[list[OfferPart], dict[str, list[int]]]:
    """
    Deduplicate items by search term.

    Returns:
        - deduplicated items (one per unique search_term)
        - mapping: {search_term: [item_ids]} for result copying
    """
    term_to_ids: dict[str, list[int]] = defaultdict(list)
    seen_terms: set[str] = set()
    unique_items: list[OfferPart] = []

    for item in items:
        term = search_terms.get(item.id, item.name)
        term_to_ids[term].append(item.id)
        if term not in seen_terms:
            seen_terms.add(term)
            unique_items.append(item)

    return unique_items, dict(term_to_ids)


def _expand_deduplicated_results(
    results_path: Path,
    term_to_ids: dict[str, list[int]],
    search_terms: dict[int, str],
) -> None:
    """
    Expand deduplicated scraper results back to all items sharing the same search term.

    Modifies the results JSON file in-place to include duplicated results for items
    that share the same search term.
    """
    with open(results_path, encoding="utf-8") as f:
        results_data = json.load(f)

    items = results_data.get("items", [])
    if not items:
        return

    # Build mapping from item_id to result
    id_to_result = {item["input"]["id"]: item for item in items}

    # Build reverse mapping: search_term -> first item_id that has that term
    term_to_source_id: dict[str, int] = {}
    for item_id, term in search_terms.items():
        if term not in term_to_source_id and item_id in id_to_result:
            term_to_source_id[term] = item_id

    # Expand results to duplicate items
    new_items = []
    for term, ids in term_to_ids.items():
        if len(ids) <= 1:
            continue

        source_id = term_to_source_id.get(term)
        if source_id is None:
            continue

        source_result = id_to_result.get(source_id)
        if source_result is None:
            continue

        # Copy result to all other items with the same search term
        for dup_id in ids:
            if dup_id != source_id and dup_id not in id_to_result:
                dup_result = source_result.copy()
                dup_result["input"] = source_result["input"].copy()
                dup_result["input"]["id"] = dup_id
                new_items.append(dup_result)

    if new_items:
        results_data["items"].extend(new_items)
        logger.info(f"Expanded {len(new_items)} duplicate results from deduplication")

        with open(results_path, "w", encoding="utf-8") as f:
            json.dump(results_data, f, indent=2)


def run_scraper_subprocess(
    input_csv: Path,
    output_dir: Path,
    run_id: str,
    headless: bool = False,
    progress_callback: Callable[[int, int], None] | None = None,
) -> Path | None:
    """Run the scraper as a subprocess with optional progress tracking."""
    import time

    progress_file = output_dir / f".scraper_progress_{run_id}.json"

    cmd = [
        sys.executable,
        "-m",
        "scraper.main",
        "--input",
        str(input_csv),
        "--output",
        str(output_dir),
        "--run-id",
        run_id,
        "--progress-file",
        str(progress_file),
    ]

    if headless:
        cmd.append("--headless")

    logger.info(f"Running scraper: {' '.join(cmd)}")

    try:
        stderr_file = tempfile.TemporaryFile(mode="w+")
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
        )

        last_completed = 0
        start_time = time.time()
        timeout = 1800  # 30 min

        while process.poll() is None:
            if time.time() - start_time > timeout:
                logger.error("Scraper timed out")
                process.kill()
                stderr_file.close()
                return None

            # Check progress file
            if progress_callback and progress_file.exists():
                try:
                    with open(progress_file, encoding="utf-8") as f:
                        progress_data = json.load(f)
                    completed = progress_data.get("completed", 0)
                    total = progress_data.get("total", 0)
                    if completed != last_completed and total > 0:
                        progress_callback(completed, total)
                        last_completed = completed
                except (json.JSONDecodeError, FileNotFoundError):
                    pass

            time.sleep(0.5)

        # Clean up progress file
        try:
            if progress_file.exists():
                progress_file.unlink()
        except Exception:
            pass

        if process.returncode != 0:
            stderr_file.seek(0)
            stderr_content = stderr_file.read().strip()
            stderr_file.close()
            if stderr_content:
                logger.error(f"Scraper stderr:\n{stderr_content}")
            logger.error(f"Scraper failed with code {process.returncode}")
            return None

        stderr_file.close()

        # Find the output file
        results_file = output_dir / f"results_{run_id}.json"
        if results_file.exists():
            return results_file

        # Try to find any matching file
        for f in output_dir.glob(f"results_{run_id}*.json"):
            return f

        logger.error("Could not find scraper output file")
        return None

    except Exception as e:
        logger.error(f"Scraper failed: {e}")
        return None


def run_processor_subprocess(
    input_csv: Path,
    results_json: Path,
    output_csv: Path,
    progress_callback: Callable[[int, int], None] | None = None,
) -> bool:
    """
    Run the processor as a subprocess with optional progress tracking.

    Args:
        input_csv: Path to input CSV
        results_json: Path to scraper results JSON
        output_csv: Path for output CSV
        progress_callback: Optional callback for progress updates (completed, total)

    Returns:
        True if successful, False otherwise
    """
    # Create a temp file for progress tracking
    progress_file = output_csv.parent / f".processor_progress_{output_csv.stem}.json"

    cmd = [
        sys.executable,
        "-m",
        "scraper.processor",
        "--input",
        str(input_csv),
        "--results",
        str(results_json),
        "--output",
        str(output_csv),
        "--progress-file",
        str(progress_file),
    ]

    logger.debug(f"Running processor: {' '.join(cmd)}")

    try:
        # Start processor subprocess
        # Redirect output to DEVNULL to avoid pipe buffer deadlock
        stderr_file = tempfile.TemporaryFile(mode="w+")
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
        )

        # Poll for progress updates while waiting
        import time

        last_completed = 0
        start_time = time.time()
        timeout = 1800  # 30 min, matches scraper timeout

        while process.poll() is None:
            if time.time() - start_time > timeout:
                logger.error("Processor timed out after 30 minutes")
                process.kill()
                stderr_file.close()
                return False

            # Check progress file
            if progress_callback and progress_file.exists():
                try:
                    with open(progress_file, encoding="utf-8") as f:
                        progress_data = json.load(f)
                    completed = progress_data.get("completed", 0)
                    total = progress_data.get("total", 0)
                    if completed != last_completed and total > 0:
                        progress_callback(completed, total)
                        last_completed = completed
                except (json.JSONDecodeError, FileNotFoundError):
                    pass  # Ignore transient file access issues

            time.sleep(0.5)  # Poll every 500ms

        # Clean up progress file
        try:
            if progress_file.exists():
                progress_file.unlink()
        except Exception:
            pass

        if process.returncode != 0:
            stderr_file.seek(0)
            stderr_content = stderr_file.read().strip()
            stderr_file.close()
            if stderr_content:
                logger.error(f"Processor stderr:\n{stderr_content}")
            logger.error(f"Processor failed with code {process.returncode}")
            return False

        stderr_file.close()
        return output_csv.exists()

    except Exception as e:
        logger.error(f"Processor failed: {e}")
        return False


class _PercentageColumn(ProgressColumn):
    """Show percentage only for tasks with a known total."""

    def render(self, task):
        if task.total is None:
            return Text("")
        return Text(f"{task.percentage:>3.0f}%", style="progress.percentage")


@contextmanager
def _redirect_logging_to_rich(console):
    """Temporarily route logging through Rich so messages display above progress bars."""
    root_logger = logging.getLogger()
    original_handlers = root_logger.handlers[:]
    for h in original_handlers:
        root_logger.removeHandler(h)
    rich_handler = RichHandler(console=console, show_path=False)
    root_logger.addHandler(rich_handler)
    try:
        yield
    finally:
        root_logger.removeHandler(rich_handler)
        for h in original_handlers:
            root_logger.addHandler(h)


def run_progress(state: WizardState, output_dir: Path) -> bool:
    """
    Run the pipeline with progress display.

    Returns:
        True if successful, False otherwise.
    """
    console = Console()
    output_dir.mkdir(parents=True, exist_ok=True)

    run_id = f"pipeline_{state.offer_id}_{state.run_timestamp}"

    with _redirect_logging_to_rich(console), Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        _PercentageColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        # Phase 1: Fetch items (if not using cached CSV)
        if not state.use_cached_csv:
            task_fetch = progress.add_task("Fetching items from database...", total=None)

            try:
                items = fetch_items(state.offer_id, limit=state.limit)
            except Exception as e:
                console.print(f"[red]Failed to fetch items: {e}[/red]")
                return False

            if not items:
                console.print("[red]No items found[/red]")
                return False

            state.items = items
            state.item_count = len(items)
            progress.update(task_fetch, total=1, completed=1)
            progress.remove_task(task_fetch)

            # Phase 2: Generate search terms with weights
            task_terms = progress.add_task(
                "Generating search terms (LLM)...", total=None
            )

            items_for_terms = [{"id": item.id, "name": item.name} for item in items]

            search_terms, weights, per_qtys, failed_ids = generate_search_terms_with_weights(
                items_for_terms,
                batch_size=state.search_term_batch_size,
            )

            state.search_terms = search_terms
            state.search_term_weights = weights
            state.search_term_per_qtys = per_qtys

            if failed_ids:
                logger.warning(
                    f"Failed to generate search terms for {len(failed_ids)} items"
                )

            progress.update(task_terms, total=1, completed=1)
            progress.remove_task(task_terms)

            # Write CSV (include offer_id in filename for cache isolation)
            temp_csv = output_dir / f"pipeline_input_{state.offer_id}_{state.run_timestamp}.csv"
            write_pipeline_csv(items, search_terms, weights, temp_csv, per_qtys=per_qtys)
            state.temp_csv_path = temp_csv
            logger.info(f"Wrote input CSV: {temp_csv}")

        else:
            # Using cached CSV - load items from it
            console.print(f"[dim]Using cached CSV: {state.temp_csv_path}[/dim]")

            # Load items from cached CSV for the scraping phase
            items = []
            search_terms = {}
            weights: dict[int, float | None] = {}
            per_qtys: dict[int, float | None] = {}

            with open(state.temp_csv_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    item_id = int(row["id"])
                    items.append(
                        OfferPart(
                            id=item_id,
                            name=row["db_name"],
                            price=int(row["our_price"]),
                            category_id=int(row["category_id"]),
                        )
                    )
                    search_terms[item_id] = row["search_term"]
                    weight_str = row.get("our_weight_g", "")
                    weights[item_id] = float(weight_str) if weight_str else None
                    per_qty_str = row.get("our_per_qty", "")
                    per_qtys[item_id] = float(per_qty_str) if per_qty_str else None

            # Apply limit if set (mirrors the DB-level limit used in fresh fetches)
            if state.limit and len(items) > state.limit:
                limited_ids = {item.id for item in items[:state.limit]}
                items = items[:state.limit]
                search_terms = {k: v for k, v in search_terms.items() if k in limited_ids}
                weights = {k: v for k, v in weights.items() if k in limited_ids}
                per_qtys = {k: v for k, v in per_qtys.items() if k in limited_ids}

                # Write a limited CSV so the processor subprocess also respects the limit
                limited_csv = output_dir / f"pipeline_input_{state.offer_id}_{state.run_timestamp}.csv"
                write_pipeline_csv(items, search_terms, weights, limited_csv, per_qtys=per_qtys)
                state.temp_csv_path = limited_csv
                logger.info(f"Wrote limited CSV ({len(items)} items): {limited_csv}")

            state.items = items
            state.search_terms = search_terms
            state.search_term_weights = weights
            state.search_term_per_qtys = per_qtys
            state.item_count = len(items)

        # Phase 3: Scrape (if not using cached results)
        if not state.use_cached_results:
            # Deduplicate items by search term
            unique_items, term_to_ids = deduplicate_for_scraping(
                state.items, state.search_terms
            )

            console.print(
                f"[dim]Scraping {len(unique_items)} unique search terms "
                f"({len(state.items)} total items)[/dim]"
            )

            scrape_total = len(unique_items) * 2  # 2 stores
            task_scrape = progress.add_task(
                "Scraping stores...", total=scrape_total
            )

            def update_scrape_progress(completed: int, total: int) -> None:
                progress.update(task_scrape, completed=completed, total=total)

            # Write deduplicated CSV for scraper
            dedup_csv = output_dir / f"pipeline_input_dedup_{state.offer_id}_{state.run_timestamp}.csv"
            write_pipeline_csv(
                unique_items,
                state.search_terms,
                state.search_term_weights,
                dedup_csv,
                per_qtys=state.search_term_per_qtys,
            )

            results_path = run_scraper_subprocess(
                dedup_csv,
                output_dir,
                run_id,
                headless=state.headless,
                progress_callback=update_scrape_progress,
            )

            if not results_path:
                console.print("[red]Scraper failed[/red]")
                return False

            # Expand deduplicated results back to all items sharing the same search term
            if len(unique_items) < len(state.items):
                _expand_deduplicated_results(results_path, term_to_ids, state.search_terms)

            state.results_path = results_path
            progress.update(task_scrape, completed=scrape_total)
            progress.remove_task(task_scrape)
            logger.info(f"Scraper complete: {results_path}")

        else:
            console.print(f"[dim]Using cached results: {state.results_path}[/dim]")

        # Phase 4: Process results with progress tracking
        # First, count items to set up progress bar
        with open(state.results_path, encoding="utf-8") as f:
            results_data = json.load(f)
        total_items = len(results_data.get("items", []))

        task_process = progress.add_task("Processing results (LLM)...", total=total_items or 1)

        comparison_csv = output_dir / f"pipeline_comparison_{state.offer_id}_{state.run_timestamp}.csv"

        def update_processor_progress(completed: int, total: int) -> None:
            progress.update(task_process, completed=completed, total=total)

        success = run_processor_subprocess(
            state.temp_csv_path,
            state.results_path,
            comparison_csv,
            progress_callback=update_processor_progress,
        )

        if not success:
            console.print("[red]Processor failed[/red]")
            return False

        state.comparison_csv_path = comparison_csv
        progress.update(task_process, completed=total_items or 1)
        progress.remove_task(task_process)
        logger.info(f"Processor complete: {comparison_csv}")

    console.print("[green]Pipeline phases complete![/green]")
    return True
