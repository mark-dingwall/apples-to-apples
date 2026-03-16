"""
Results processor: Uses Claude CLI to evaluate matches, parse weights, convert prices.
Outputs comparison CSV.
"""

import argparse
import csv
import json
import logging
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

try:
    from scraper.db_schema import CATEGORY_NAMES
except ImportError:
    raise ImportError(
        "Database schema not found. Copy scraper/db_schema.example.py "
        "to scraper/db_schema.py and customise for your database."
    )

try:
    from scraper.stores.store_config import STORE_A_NAME, STORE_B_NAME, STORE_A_COL, STORE_B_COL
except ImportError:
    raise ImportError("Store config not found. Copy store_config.example.py to store_config.py.")

try:
    from scraper.prompts import ITEM_EVALUATION_PROMPT
except ImportError:
    raise ImportError(
        "Prompt templates not found. Copy scraper/prompts.example.py "
        "to scraper/prompts.py and customise for your use case."
    )

# Pre-compiled regex patterns for performance
RE_WEIGHT_KG = re.compile(r"(\d+(?:\.\d+)?)\s*kg", re.IGNORECASE)
RE_WEIGHT_G = re.compile(r"(\d+(?:\.\d+)?)\s*g(?!r)", re.IGNORECASE)  # g but not "gr" (grapes)
RE_UNIT_PRICE = re.compile(r"\$(\d+(?:\.\d+)?)\s*/?\s*(?:per\s+)?(\d*\s*\w+)", re.IGNORECASE)
RE_PRICE = re.compile(r"\$(\d+(?:\.\d+)?)")


@dataclass
class ProcessorInputItem:
    """Our item from the database (processor-specific format)."""

    id: int
    category_id: int
    db_name: str
    search_term: str
    our_price_cents: int
    our_weight_g: float | None = None  # Weight from LLM extraction (optional)
    our_per_qty: float | None = None  # From search-term-gen: quantity if sold by qty, None if by weight


@dataclass
class ProcessedResult:
    """A processed result ready for CSV output."""
    name: str
    raw_price: str
    unit_price: str
    converted_cents: int | None
    match_quality: Literal["good", "ok", "poor", "none"]
    weight_used_g: float | None
    is_estimate: bool
    url: str
    is_special: bool
    pack_quantity: float = 1.0  # From LLM evaluation


@dataclass
class ComparisonRow:
    """A row in the output CSV."""
    id: int
    db_name: str
    our_price_cents: int
    our_weight_g: float | None
    our_pack_quantity: float  # From LLM evaluation
    our_price_per_item: int   # Calculated: our_price_cents / our_pack_quantity
    store_a: ProcessedResult | None
    store_b: ProcessedResult | None
    store_a_qty_multiplier: float = 1.0   # LLM-determined multiplier for fair comparison
    store_a_per_qty: float | None = None       # Store pack qty from Sonnet (None = by weight)
    store_a_per_weight_g: float | None = None  # Store package weight from Sonnet (None = by qty)
    store_a_conversion_method: str = "llm"     # "math_weight", "math_unit", or "llm"
    store_b_qty_multiplier: float = 1.0
    store_b_per_qty: float | None = None
    store_b_per_weight_g: float | None = None
    store_b_conversion_method: str = "llm"


def load_input_items(csv_path: Path) -> dict[int, ProcessorInputItem]:
    """Load items from input CSV."""
    items = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Parse weight from CSV if available (from LLM extraction)
            weight_g = None
            if row.get("our_weight_g"):
                try:
                    weight_g = float(row["our_weight_g"])
                except ValueError:
                    pass

            # Parse per_qty from CSV if available
            per_qty = None
            if row.get("our_per_qty"):
                try:
                    per_qty = float(row["our_per_qty"])
                except ValueError:
                    pass

            item = ProcessorInputItem(
                id=int(row["id"]),
                category_id=int(row["category_id"]),
                db_name=row["db_name"],
                search_term=row["search_term"],
                our_price_cents=int(row["our_price"]),
                our_weight_g=weight_g,
                our_per_qty=per_qty,
            )
            items[item.id] = item
    return items


def load_results(json_path: Path) -> dict:
    """Load scraper results from JSON."""
    with open(json_path, encoding="utf-8") as f:
        return json.load(f)


def parse_weight_from_text(text: str) -> tuple[float | None, bool]:
    """
    Extract weight in grams from text.
    Returns (weight_g, is_estimate).
    """
    if not text:
        return None, True

    # Try to find weight patterns
    # Patterns: "1kg", "500g", "approx. 157g", "per 100g", "200g punnet"

    # Kilogram patterns
    kg_match = RE_WEIGHT_KG.search(text)
    if kg_match:
        return float(kg_match.group(1)) * 1000, False

    # Gram patterns
    g_match = RE_WEIGHT_G.search(text)
    if g_match:
        return float(g_match.group(1)), False

    return None, True


def parse_unit_price(unit_price_str: str) -> tuple[float | None, str | None]:
    """
    Parse unit price string like "$5.90/1kg" or "$1.26 / 1EA".
    Returns (price_dollars, unit).
    """
    if not unit_price_str:
        return None, None

    # Patterns: "$5.90/ 1kg", "$5.90/1kg", "$1.26 / 1EA"
    match = RE_UNIT_PRICE.search(unit_price_str)
    if match:
        price = float(match.group(1))
        unit = match.group(2).strip().lower()
        return price, unit

    return None, None


def convert_to_our_unit(
    unit_price_dollars: float,
    unit: str,
    our_weight_g: float | None,
) -> tuple[int | None, float | None, bool]:
    """
    Convert competitor price to our unit.
    Returns (price_cents, weight_used_g, is_estimate).
    """
    if unit in ("1kg", "kg"):
        # Per kg pricing
        if our_weight_g:
            # Convert: price_per_kg * (our_weight_g / 1000)
            price_dollars = unit_price_dollars * (our_weight_g / 1000)
            return round(price_dollars * 100), our_weight_g, False
        else:
            # No weight info, can't convert
            return None, None, True

    elif unit in ("1ea", "ea", "each"):
        # Per-each pricing - use as-is
        return round(unit_price_dollars * 100), None, False

    elif unit.startswith("100g") or unit == "100g":
        # Per 100g pricing
        if our_weight_g:
            price_dollars = unit_price_dollars * (our_weight_g / 100)
            return round(price_dollars * 100), our_weight_g, False
        else:
            return None, None, True

    else:
        # Unknown unit
        return None, None, True


def compute_guardrail_multiplier(
    unit_price_str: str | None,
    our_weight_g: float | None,
    our_per_qty: float | None,
    llm_our_pack_qty: float | None,
    llm_store_per_qty: float | None,
    llm_qty_multiplier: float,
    tolerance: float = 0.01,
) -> tuple[float, str]:
    """
    Attempt to compute qty_multiplier mathematically.

    Returns (multiplier, method) where method is:
    - "math_weight": per-weight guardrail applied (multiplier=1.0)
    - "math_unit": per-unit guardrail applied (multiplier=our_per_qty/store_per_qty)
    - "llm": fell back to LLM value
    """
    _, unit = parse_unit_price(unit_price_str) if unit_price_str else (None, None)

    # Per-weight guardrail: supermarket is per-kg/100g and we have weight
    if unit in ("1kg", "kg", "100g") and our_weight_g is not None:
        return 1.0, "math_weight"

    # Per-unit guardrail: supermarket is per-each and we know both pack qtys
    if unit in ("1ea", "ea", "each") and our_per_qty is not None and our_per_qty > 0:
        # Cross-validate: search-term-gen and Sonnet must agree on our pack qty
        if (llm_our_pack_qty is not None
                and abs(our_per_qty - llm_our_pack_qty) < tolerance
                and llm_store_per_qty is not None
                and llm_store_per_qty > 0):
            return our_per_qty / llm_store_per_qty, "math_unit"

    return llm_qty_multiplier, "llm"


def prepare_batch_data(
    item_results: list[dict],
    input_items: dict[int, ProcessorInputItem],
) -> list[dict]:
    """Prepare batch data for LLM evaluation."""
    batch = []

    for item_result in item_results:
        input_data = item_result["input"]
        item_id = input_data["id"]

        if item_id not in input_items:
            continue

        our_item = input_items[item_id]

        item_data = {
            "id": item_id,
            "our_name": our_item.db_name,
            "search_term": our_item.search_term,
            "category": CATEGORY_NAMES.get(our_item.category_id, "produce").lower(),
            "our_weight_g": our_item.our_weight_g,
            "our_per_qty": our_item.our_per_qty,
            "store_a_results": [],
            "store_b_results": [],
        }

        # Add Store A results
        store_a_data = item_result.get(STORE_A_COL, {})
        if store_a_data.get("status") == "success" and store_a_data.get("results"):
            for r in store_a_data["results"]:
                item_data["store_a_results"].append({
                    "rank": r["rank"],
                    "name": r["name"],
                    "price": r["price"],
                    "unit_price": r["unit_price"],
                })

        # Add Store B results
        store_b_data = item_result.get(STORE_B_COL, {})
        if store_b_data.get("status") == "success" and store_b_data.get("results"):
            for r in store_b_data["results"]:
                item_data["store_b_results"].append({
                    "rank": r["rank"],
                    "name": r["name"],
                    "price": r["price"],
                    "unit_price": r["unit_price"],
                })

        batch.append(item_data)

    return batch


def build_item_prompt(item_data: dict) -> str:
    """Build a prompt for evaluating a single item."""
    item_json = json.dumps(item_data, indent=2)
    return ITEM_EVALUATION_PROMPT.format(item_json=item_json, item_id=item_data['id'])


def evaluate_single_item(item_data: dict, timeout: int = 60) -> dict | None:
    """
    Evaluate a single item using Claude Sonnet.

    Returns the evaluation dict or None on failure.
    """
    prompt = build_item_prompt(item_data)
    item_id = item_data["id"]

    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "sonnet"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        if result.returncode != 0:
            logger.warning(f"Item {item_id}: Claude CLI error (rc={result.returncode}): {result.stderr}")
            return None

        response = result.stdout.strip()

        # Parse JSON from response
        json_match = re.search(r"\{[\s\S]*\}", response)
        if not json_match:
            logger.warning(f"Item {item_id}: No JSON found in response")
            return None

        data = json.loads(json_match.group())

        # Validate the response has required fields
        if data.get("id") != item_id:
            logger.warning(f"Item {item_id}: Response ID mismatch")
            return None

        return data

    except subprocess.TimeoutExpired:
        logger.warning(f"Item {item_id}: Timed out")
        return None
    except json.JSONDecodeError as e:
        logger.warning(f"Item {item_id}: JSON parse error: {e}")
        return None
    except Exception as e:
        logger.error(f"Item {item_id}: Unexpected error: {e}", exc_info=True)
        return None


def run_parallel_evaluations(
    items_data: list[dict],
    max_workers: int = 8,
    progress_file: Path | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[list[dict], list[int]]:
    """
    Evaluate items in parallel using ThreadPoolExecutor.

    Args:
        items_data: List of item data dicts to evaluate
        max_workers: Number of parallel workers (default 8)
        progress_file: Optional path to write progress updates
        on_progress: Optional callback for progress updates (completed, total)

    Returns:
        Tuple of (list of successful evaluations, list of failed item IDs)
    """
    total_items = len(items_data)
    if total_items == 0:
        return [], []

    evaluations = []
    failures = []
    completed_count = 0

    def update_progress() -> None:
        if on_progress:
            on_progress(completed_count, total_items)

        if progress_file:
            try:
                with open(progress_file, "w", encoding="utf-8") as f:
                    json.dump({
                        "completed": completed_count,
                        "total": total_items,
                        "failed": len(failures),
                    }, f)
            except Exception:
                pass

    logger.info(f"Evaluating {total_items} items with {max_workers} parallel workers...")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all tasks
        future_to_item = {
            executor.submit(evaluate_single_item, item): item
            for item in items_data
        }

        # Collect results as they complete
        for future in as_completed(future_to_item):
            item = future_to_item[future]
            item_id = item["id"]

            try:
                result = future.result()
                if result:
                    evaluations.append(result)
                else:
                    failures.append(item_id)
            except Exception as e:
                logger.debug(f"Item {item_id}: Exception: {e}")
                failures.append(item_id)

            completed_count += 1
            update_progress()

    logger.info(f"Completed: {len(evaluations)} successful, {len(failures)} failed")
    return evaluations, failures


def process_result(
    result: dict,
    match_quality: str,
    our_weight_g: float | None,
    pack_quantity: float = 1.0,
) -> ProcessedResult:
    """Process a single scraped result into a ProcessedResult."""
    unit_price_dollars, unit = parse_unit_price(result["unit_price"])

    converted_cents = None
    weight_used_g = None
    is_estimate = True

    if unit_price_dollars and unit:
        converted_cents, weight_used_g, is_estimate = convert_to_our_unit(
            unit_price_dollars, unit, our_weight_g
        )

    # For per-each items, use the raw (total) price instead of per-each unit price.
    # Unit price gives per-unit cost (e.g. $1.10/ea for a 5-pack), but we need the
    # pack price ($5.50) so qty_multiplier (our_qty/store_qty) works correctly.
    if unit in ("1ea", "ea", "each"):
        price_match = RE_PRICE.search(result["price"])
        if price_match:
            converted_cents = round(float(price_match.group(1)) * 100)
            is_estimate = False

    return ProcessedResult(
        name=result["name"],
        raw_price=result["price"],
        unit_price=result["unit_price"],
        converted_cents=converted_cents,
        match_quality=match_quality,
        weight_used_g=weight_used_g,
        is_estimate=is_estimate,
        url=result["url"],
        is_special=result.get("is_on_special", False),
        pack_quantity=pack_quantity,
    )


def process_items(
    input_items: dict[int, ProcessorInputItem],
    results_data: dict,
    progress_file: Path | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    max_workers: int = 8,
) -> tuple[list[ComparisonRow], list[int]]:
    """
    Process all items and return comparison rows.

    Args:
        input_items: Items from input CSV
        results_data: Scraper results
        progress_file: Optional path for progress file
        on_progress: Optional callback for progress updates
        max_workers: Number of parallel workers (default 8)

    Returns:
        Tuple of (list of comparison rows, list of item_ids that failed LLM evaluation)
    """
    from scraper.wizard.settings import SettingsManager
    guardrail_tolerance = SettingsManager().load().guardrail_tolerance

    all_item_results = results_data["items"]

    # Prepare batch data for all items
    batch_data = prepare_batch_data(all_item_results, input_items)

    # Filter to items with results
    items_with_results = [b for b in batch_data if b["store_a_results"] or b["store_b_results"]]

    # Run parallel evaluations
    all_evaluations = {}
    all_failed_ids: list[int] = []

    if items_with_results:
        evaluations_list, all_failed_ids = run_parallel_evaluations(
            items_with_results,
            max_workers=max_workers,
            progress_file=progress_file,
            on_progress=on_progress,
        )

        # Convert list to dict keyed by item_id
        for eval_item in evaluations_list:
            item_id = eval_item.get("id")
            if item_id is not None:
                all_evaluations[item_id] = {
                    "our_pack_qty": eval_item.get("our_pack_qty", 1),
                    "store_a_rank": eval_item.get("store_a_match_rank"),
                    "store_a_quality": eval_item.get("store_a_quality", "none"),
                    "store_a_pack_qty": eval_item.get("store_a_pack_qty", 1),
                    "store_a_per_qty": eval_item.get("store_a_per_qty"),
                    "store_a_per_weight_g": eval_item.get("store_a_per_weight_g"),
                    "store_a_qty_multiplier": eval_item.get("store_a_qty_multiplier", 1) or 1,
                    "store_b_rank": eval_item.get("store_b_match_rank"),
                    "store_b_quality": eval_item.get("store_b_quality", "none"),
                    "store_b_pack_qty": eval_item.get("store_b_pack_qty", 1),
                    "store_b_per_qty": eval_item.get("store_b_per_qty"),
                    "store_b_per_weight_g": eval_item.get("store_b_per_weight_g"),
                    "store_b_qty_multiplier": eval_item.get("store_b_qty_multiplier", 1) or 1,
                }

    # Build comparison rows
    rows = []

    for item_result in all_item_results:
        input_data = item_result["input"]
        item_id = input_data["id"]

        if item_id not in input_items:
            logger.warning(f"Item {item_id} not found in input CSV, skipping")
            continue

        our_item = input_items[item_id]
        # Use weight from CSV (LLM extraction) if available, else fall back to regex
        our_weight_g = our_item.our_weight_g
        if our_weight_g is None:
            our_weight_g, _ = parse_weight_from_text(our_item.db_name)

        evaluation = all_evaluations.get(item_id, {})

        # Process Store A
        store_a_result = None
        store_a_data = item_result.get(STORE_A_COL, {})
        store_a_rank = evaluation.get("store_a_rank")
        store_a_quality = evaluation.get("store_a_quality", "none")
        store_a_pack_qty = evaluation.get("store_a_pack_qty", 1) or 1
        store_a_per_qty = evaluation.get("store_a_per_qty")
        store_a_per_weight_g = evaluation.get("store_a_per_weight_g")
        store_a_llm_qty_mult = evaluation.get("store_a_qty_multiplier", 1) or 1
        store_a_conversion_method = "llm"
        store_a_qty_mult = store_a_llm_qty_mult

        if (
            store_a_rank is not None
            and 1 <= store_a_rank <= len(store_a_data.get("results", []))
            and store_a_quality != "none"
        ):
            result = store_a_data["results"][store_a_rank - 1]  # Convert to 0-based
            store_a_result = process_result(result, store_a_quality, our_weight_g, store_a_pack_qty)

            # Apply guardrail
            store_a_qty_mult, store_a_conversion_method = compute_guardrail_multiplier(
                unit_price_str=result.get("unit_price"),
                our_weight_g=our_weight_g,
                our_per_qty=our_item.our_per_qty,
                llm_our_pack_qty=evaluation.get("our_pack_qty"),
                llm_store_per_qty=store_a_per_qty,
                llm_qty_multiplier=store_a_llm_qty_mult,
                tolerance=guardrail_tolerance,
            )
            if store_a_conversion_method != "llm":
                logger.info(
                    f"  Item {item_id} {STORE_A_NAME}: guardrail {store_a_conversion_method} "
                    f"override {store_a_llm_qty_mult} -> {store_a_qty_mult}"
                )
            # Sync pack_qty with per_qty when guardrail validates per-unit math
            if store_a_conversion_method == "math_unit" and store_a_per_qty is not None:
                store_a_result.pack_quantity = store_a_per_qty

        # Process Store B
        store_b_result = None
        store_b_data = item_result.get(STORE_B_COL, {})
        store_b_rank = evaluation.get("store_b_rank")
        store_b_quality = evaluation.get("store_b_quality", "none")
        store_b_pack_qty = evaluation.get("store_b_pack_qty", 1) or 1
        store_b_per_qty = evaluation.get("store_b_per_qty")
        store_b_per_weight_g = evaluation.get("store_b_per_weight_g")
        store_b_llm_qty_mult = evaluation.get("store_b_qty_multiplier", 1) or 1
        store_b_conversion_method = "llm"
        store_b_qty_mult = store_b_llm_qty_mult

        if (
            store_b_rank is not None
            and 1 <= store_b_rank <= len(store_b_data.get("results", []))
            and store_b_quality != "none"
        ):
            result = store_b_data["results"][store_b_rank - 1]  # Convert to 0-based
            store_b_result = process_result(result, store_b_quality, our_weight_g, store_b_pack_qty)

            # Apply guardrail
            store_b_qty_mult, store_b_conversion_method = compute_guardrail_multiplier(
                unit_price_str=result.get("unit_price"),
                our_weight_g=our_weight_g,
                our_per_qty=our_item.our_per_qty,
                llm_our_pack_qty=evaluation.get("our_pack_qty"),
                llm_store_per_qty=store_b_per_qty,
                llm_qty_multiplier=store_b_llm_qty_mult,
                tolerance=guardrail_tolerance,
            )
            if store_b_conversion_method != "llm":
                logger.info(
                    f"  Item {item_id} {STORE_B_NAME}: guardrail {store_b_conversion_method} "
                    f"override {store_b_llm_qty_mult} -> {store_b_qty_mult}"
                )
            # Sync pack_qty with per_qty when guardrail validates per-unit math
            if store_b_conversion_method == "math_unit" and store_b_per_qty is not None:
                store_b_result.pack_quantity = store_b_per_qty

        # Calculate our per-item price
        our_pack_qty = evaluation.get("our_pack_qty", 1) or 1
        our_per_item = round(our_item.our_price_cents / our_pack_qty)

        rows.append(ComparisonRow(
            id=item_id,
            db_name=our_item.db_name,
            our_price_cents=our_item.our_price_cents,
            our_weight_g=our_weight_g,
            our_pack_quantity=our_pack_qty,
            our_price_per_item=our_per_item,
            store_a=store_a_result,
            store_b=store_b_result,
            store_a_qty_multiplier=store_a_qty_mult,
            store_a_per_qty=store_a_per_qty,
            store_a_per_weight_g=store_a_per_weight_g,
            store_a_conversion_method=store_a_conversion_method,
            store_b_qty_multiplier=store_b_qty_mult,
            store_b_per_qty=store_b_per_qty,
            store_b_per_weight_g=store_b_per_weight_g,
            store_b_conversion_method=store_b_conversion_method,
        ))

        logger.info(
            f"  {our_item.db_name}: "
            f"{STORE_A_NAME}={store_a_result.match_quality if store_a_result else 'none'}, "
            f"{STORE_B_NAME}={store_b_result.match_quality if store_b_result else 'none'}"
        )

    if all_failed_ids:
        logger.warning(f"Total items that failed LLM evaluation: {len(all_failed_ids)}")

    return rows, all_failed_ids


def calculate_percentage_diff(
    our_per_item: int,
    their_cents: int | None,
    their_pack_qty: float = 1.0,
) -> str:
    """Calculate percentage difference using per-item prices."""
    if their_cents is None or our_per_item == 0:
        return ""
    their_per_item = their_cents / their_pack_qty
    diff_pct = ((their_per_item - our_per_item) / our_per_item) * 100
    return f"{diff_pct:+.1f}%"


def determine_cheapest(
    our_per_item: int,
    store_a_cents: int | None,
    store_a_pack_qty: float,
    store_b_cents: int | None,
    store_b_pack_qty: float,
) -> str:
    """Determine which is cheapest using per-item prices."""
    prices = {"ours": our_per_item}
    if store_a_cents is not None:
        prices["store_a"] = store_a_cents / store_a_pack_qty
    if store_b_cents is not None:
        prices["store_b"] = store_b_cents / store_b_pack_qty

    if len(prices) == 1:
        return ""

    cheapest = min(prices, key=prices.get)
    return cheapest


def create_verify_formula(unit_price: str, weight_g: float | None, converted_cents: int | None) -> str:
    """Create Excel formula to verify conversion."""
    if not unit_price or converted_cents is None:
        return ""

    # Parse the unit price
    match = RE_UNIT_PRICE.search(unit_price)
    if not match:
        return ""

    price = float(match.group(1))
    unit = match.group(2).strip().lower()

    if unit in ("1kg", "kg") and weight_g:
        expected = round(price * (weight_g / 1000) * 100)
        return f'=IF({expected}={converted_cents},"OK","ERR exp {expected}")'
    elif unit in ("1ea", "ea", "each"):
        expected = round(price * 100)
        return f'=IF({expected}={converted_cents},"OK","ERR exp {expected}")'
    elif unit in ("100g",) and weight_g:
        expected = round(price * (weight_g / 100) * 100)
        return f'=IF({expected}={converted_cents},"OK","ERR exp {expected}")'

    return ""


def write_csv(rows: list[ComparisonRow], output_path: Path) -> None:
    """Write comparison results to CSV."""
    _a = STORE_A_COL
    _b = STORE_B_COL
    fieldnames = [
        "id", "db_name", "our_price_cents", "our_weight_g",
        "our_pack_quantity", "our_price_per_item",
        f"{_a}_name", f"{_a}_raw_price", f"{_a}_unit_price", f"{_a}_converted_cents",
        f"{_a}_pack_quantity", f"{_a}_price_per_item", f"{_a}_qty_multiplier",
        f"{_a}_per_qty", f"{_a}_per_weight_g", f"{_a}_conversion_method",
        f"{_a}_match_quality", f"{_a}_weight_used_g", f"{_a}_is_estimate", f"{_a}_url",
        f"{_b}_name", f"{_b}_raw_price", f"{_b}_unit_price", f"{_b}_converted_cents",
        f"{_b}_pack_quantity", f"{_b}_price_per_item", f"{_b}_qty_multiplier",
        f"{_b}_per_qty", f"{_b}_per_weight_g", f"{_b}_conversion_method",
        f"{_b}_match_quality", f"{_b}_weight_used_g", f"{_b}_is_estimate", f"{_b}_url",
        f"our_vs_{_a}_pct", f"our_vs_{_b}_pct", "cheapest", f"verify_{_a}", f"verify_{_b}",
    ]

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for row in rows:
            csv_row = {
                "id": row.id,
                "db_name": row.db_name,
                "our_price_cents": row.our_price_cents,
                "our_weight_g": row.our_weight_g or "",
                "our_pack_quantity": row.our_pack_quantity,
                "our_price_per_item": row.our_price_per_item,
            }

            # Store A columns
            store_a_pack_qty = row.store_a.pack_quantity if row.store_a else 1.0
            store_a_cents = row.store_a.converted_cents if row.store_a else None
            store_a_per_item = round(store_a_cents / store_a_pack_qty) if store_a_cents is not None else ""

            if row.store_a:
                csv_row.update({
                    f"{_a}_name": row.store_a.name,
                    f"{_a}_raw_price": row.store_a.raw_price,
                    f"{_a}_unit_price": row.store_a.unit_price,
                    f"{_a}_converted_cents": row.store_a.converted_cents if row.store_a.converted_cents is not None else "",
                    f"{_a}_pack_quantity": row.store_a.pack_quantity,
                    f"{_a}_price_per_item": store_a_per_item,
                    f"{_a}_qty_multiplier": row.store_a_qty_multiplier,
                    f"{_a}_per_qty": row.store_a_per_qty if row.store_a_per_qty is not None else "",
                    f"{_a}_per_weight_g": row.store_a_per_weight_g if row.store_a_per_weight_g is not None else "",
                    f"{_a}_conversion_method": row.store_a_conversion_method,
                    f"{_a}_match_quality": row.store_a.match_quality,
                    f"{_a}_weight_used_g": row.store_a.weight_used_g or "",
                    f"{_a}_is_estimate": row.store_a.is_estimate,
                    f"{_a}_url": row.store_a.url,
                })
            else:
                csv_row.update({
                    f"{_a}_name": "",
                    f"{_a}_raw_price": "",
                    f"{_a}_unit_price": "",
                    f"{_a}_converted_cents": "",
                    f"{_a}_pack_quantity": "",
                    f"{_a}_price_per_item": "",
                    f"{_a}_qty_multiplier": "",
                    f"{_a}_per_qty": "",
                    f"{_a}_per_weight_g": "",
                    f"{_a}_conversion_method": "",
                    f"{_a}_match_quality": "none",
                    f"{_a}_weight_used_g": "",
                    f"{_a}_is_estimate": "",
                    f"{_a}_url": "",
                })

            # Store B columns
            store_b_pack_qty = row.store_b.pack_quantity if row.store_b else 1.0
            store_b_cents = row.store_b.converted_cents if row.store_b else None
            store_b_per_item = round(store_b_cents / store_b_pack_qty) if store_b_cents is not None else ""

            if row.store_b:
                csv_row.update({
                    f"{_b}_name": row.store_b.name,
                    f"{_b}_raw_price": row.store_b.raw_price,
                    f"{_b}_unit_price": row.store_b.unit_price,
                    f"{_b}_converted_cents": row.store_b.converted_cents if row.store_b.converted_cents is not None else "",
                    f"{_b}_pack_quantity": row.store_b.pack_quantity,
                    f"{_b}_price_per_item": store_b_per_item,
                    f"{_b}_qty_multiplier": row.store_b_qty_multiplier,
                    f"{_b}_per_qty": row.store_b_per_qty if row.store_b_per_qty is not None else "",
                    f"{_b}_per_weight_g": row.store_b_per_weight_g if row.store_b_per_weight_g is not None else "",
                    f"{_b}_conversion_method": row.store_b_conversion_method,
                    f"{_b}_match_quality": row.store_b.match_quality,
                    f"{_b}_weight_used_g": row.store_b.weight_used_g or "",
                    f"{_b}_is_estimate": row.store_b.is_estimate,
                    f"{_b}_url": row.store_b.url,
                })
            else:
                csv_row.update({
                    f"{_b}_name": "",
                    f"{_b}_raw_price": "",
                    f"{_b}_unit_price": "",
                    f"{_b}_converted_cents": "",
                    f"{_b}_pack_quantity": "",
                    f"{_b}_price_per_item": "",
                    f"{_b}_qty_multiplier": "",
                    f"{_b}_per_qty": "",
                    f"{_b}_per_weight_g": "",
                    f"{_b}_conversion_method": "",
                    f"{_b}_match_quality": "none",
                    f"{_b}_weight_used_g": "",
                    f"{_b}_is_estimate": "",
                    f"{_b}_url": "",
                })

            # Comparison columns - use per-item prices
            csv_row[f"our_vs_{_a}_pct"] = calculate_percentage_diff(
                row.our_price_per_item, store_a_cents, store_a_pack_qty
            )
            csv_row[f"our_vs_{_b}_pct"] = calculate_percentage_diff(
                row.our_price_per_item, store_b_cents, store_b_pack_qty
            )
            csv_row["cheapest"] = determine_cheapest(
                row.our_price_per_item, store_a_cents, store_a_pack_qty, store_b_cents, store_b_pack_qty
            )

            # Verification formulas
            csv_row[f"verify_{_a}"] = create_verify_formula(
                row.store_a.unit_price if row.store_a else "",
                row.our_weight_g,
                store_a_cents,
            )
            csv_row[f"verify_{_b}"] = create_verify_formula(
                row.store_b.unit_price if row.store_b else "",
                row.our_weight_g,
                store_b_cents,
            )

            writer.writerow(csv_row)

    logger.info(f"Wrote {len(rows)} rows to {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Process scraper results using Claude CLI")
    parser.add_argument("--input", required=True, help="Path to input CSV (items.csv)")
    parser.add_argument("--results", required=True, help="Path to results JSON")
    parser.add_argument("--output", required=True, help="Output CSV path")
    parser.add_argument("--progress-file", help="Path to write progress updates (JSON)")
    parser.add_argument("--max-workers", type=int, default=8, help="Number of parallel workers (default: 8)")

    args = parser.parse_args()

    input_path = Path(args.input)
    results_path = Path(args.results)
    output_path = Path(args.output)
    progress_file = Path(args.progress_file) if args.progress_file else None

    if not input_path.exists():
        logger.error(f"Input CSV not found: {input_path}")
        sys.exit(1)

    if not results_path.exists():
        logger.error(f"Results JSON not found: {results_path}")
        sys.exit(1)

    # Create output directory if needed
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Load data
    logger.info(f"Loading input items from {input_path}")
    input_items = load_input_items(input_path)
    logger.info(f"Loaded {len(input_items)} input items")

    logger.info(f"Loading results from {results_path}")
    results_data = load_results(results_path)
    logger.info(f"Loaded {len(results_data['items'])} result items")

    # Process items using parallel Sonnet calls
    logger.info(f"Processing items with {args.max_workers} parallel workers...")
    rows, failed_evaluation_ids = process_items(
        input_items,
        results_data,
        progress_file=progress_file,
        max_workers=args.max_workers,
    )

    # Write output
    write_csv(rows, output_path)

    # Print summary
    total = len(rows)
    store_a_matches = sum(1 for r in rows if r.store_a and r.store_a.match_quality != "none")
    store_b_matches = sum(1 for r in rows if r.store_b and r.store_b.match_quality != "none")

    print(f"\n{'='*50}")
    print("PROCESSING COMPLETE")
    print(f"{'='*50}")
    print(f"Total items: {total}")
    print(f"{STORE_A_NAME} matches: {store_a_matches}")
    print(f"{STORE_B_NAME} matches: {store_b_matches}")
    if failed_evaluation_ids:
        print(f"LLM evaluation failures: {len(failed_evaluation_ids)}")
    print(f"Output: {output_path}")


if __name__ == "__main__":
    main()
