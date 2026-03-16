"""Step 5: Approval using existing TUI."""

import csv
import logging
import re
from pathlib import Path
from typing import Literal

from scraper.db import fetch_current_rrp
from scraper.stores.store_config import STORE_A_COL, STORE_B_COL
from scraper.tui import UpdateRow, show_approval_tui
from scraper.wizard.settings import SettingsManager
from scraper.wizard.state import WizardState

logger = logging.getLogger(__name__)

# Local copy of unit price regex (same pattern as processor.py)
_RE_UNIT_PRICE = re.compile(r"\$(\d+(?:\.\d+)?)\s*/?\s*(?:per\s+)?(\d*\s*\w+)", re.IGNORECASE)


def _safe_float(value: str | None) -> float | None:
    """Safely parse a float value, returning None on failure."""
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def build_conversion_desc(
    unit_price_str: str | None,
    converted_cents: int | None,
    qty_multiplier: float,
    our_weight_g: float | None,
) -> str | None:
    """
    Build a human-readable conversion description for a single store.

    Returns a string like "$5.90/kg x 250g = $1.48" or "$2.00/ea x 3 = $6.00".
    """
    if not unit_price_str or converted_cents is None:
        return None

    match = _RE_UNIT_PRICE.search(unit_price_str)
    if not match:
        return None

    price = float(match.group(1))
    unit = match.group(2).strip().lower()

    if unit in ("1kg", "kg"):
        if our_weight_g:
            weight_result = converted_cents / 100
            if abs(qty_multiplier - 1.0) > 0.01:
                adjusted = round(converted_cents * qty_multiplier) / 100
                return f"${price:.2f}/kg x {our_weight_g:g}g = ${weight_result:.2f} x {qty_multiplier:g} = ${adjusted:.2f}"
            return f"${price:.2f}/kg x {our_weight_g:g}g = ${weight_result:.2f}"
        return None
    elif unit.startswith("100g") or unit == "100g":
        if our_weight_g:
            weight_result = converted_cents / 100
            if abs(qty_multiplier - 1.0) > 0.01:
                adjusted = round(converted_cents * qty_multiplier) / 100
                return f"${price:.2f}/100g x {our_weight_g:g}g = ${weight_result:.2f} x {qty_multiplier:g} = ${adjusted:.2f}"
            return f"${price:.2f}/100g x {our_weight_g:g}g = ${weight_result:.2f}"
        return None
    elif unit in ("1ea", "ea", "each"):
        if abs(qty_multiplier - 1.0) > 0.01:
            adjusted = round(converted_cents * qty_multiplier) / 100
            return f"${price:.2f}/ea x {qty_multiplier:g} = ${adjusted:.2f}"
        return None
    else:
        return None


def parse_comparison_csv(csv_path: Path) -> list[dict]:
    """Parse the processor output CSV."""
    results = []

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row in reader:
            store_a_cents = None
            if row.get(f"{STORE_A_COL}_converted_cents"):
                try:
                    store_a_cents = int(row[f"{STORE_A_COL}_converted_cents"])
                except ValueError as e:
                    logger.warning(f"Invalid {STORE_A_COL}_converted_cents '{row.get(f'{STORE_A_COL}_converted_cents')}': {e}")

            store_b_cents = None
            if row.get(f"{STORE_B_COL}_converted_cents"):
                try:
                    store_b_cents = int(row[f"{STORE_B_COL}_converted_cents"])
                except ValueError as e:
                    logger.warning(f"Invalid {STORE_B_COL}_converted_cents '{row.get(f'{STORE_B_COL}_converted_cents')}': {e}")

            results.append(
                {
                    "id": int(row["id"]),
                    "db_name": row["db_name"],
                    "our_price_cents": int(row["our_price_cents"]),
                    "store_a_converted_cents": store_a_cents,
                    "store_a_match_quality": row.get(f"{STORE_A_COL}_match_quality", "none"),
                    "store_a_name": row.get(f"{STORE_A_COL}_name") or None,
                    "store_a_weight_used_g": _safe_float(row.get(f"{STORE_A_COL}_weight_used_g")),
                    "store_a_unit_price": row.get(f"{STORE_A_COL}_unit_price") or None,
                    "store_a_raw_price": row.get(f"{STORE_A_COL}_raw_price") or None,
                    "store_a_pack_quantity": _safe_float(row.get(f"{STORE_A_COL}_pack_quantity")),
                    "store_a_qty_multiplier": _safe_float(row.get(f"{STORE_A_COL}_qty_multiplier")) or 1.0,
                    "store_b_converted_cents": store_b_cents,
                    "store_b_match_quality": row.get(f"{STORE_B_COL}_match_quality", "none"),
                    "store_b_name": row.get(f"{STORE_B_COL}_name") or None,
                    "store_b_weight_used_g": _safe_float(row.get(f"{STORE_B_COL}_weight_used_g")),
                    "store_b_unit_price": row.get(f"{STORE_B_COL}_unit_price") or None,
                    "store_b_raw_price": row.get(f"{STORE_B_COL}_raw_price") or None,
                    "store_b_pack_quantity": _safe_float(row.get(f"{STORE_B_COL}_pack_quantity")),
                    "store_b_qty_multiplier": _safe_float(row.get(f"{STORE_B_COL}_qty_multiplier")) or 1.0,
                    "our_weight_g": _safe_float(row.get("our_weight_g")),
                    "our_pack_quantity": _safe_float(row.get("our_pack_quantity")),
                }
            )

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

    # Both have valid results - compare quality
    store_a_rank = quality_ranking.get(store_a_quality, 0)
    store_b_rank = quality_ranking.get(store_b_quality, 0)

    if store_a_rank == store_b_rank:
        # Same quality: use max (standard RRP)
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


def build_updates(comparisons: list[dict]) -> list[UpdateRow]:
    """Build update rows from comparison results."""
    settings = SettingsManager().load()

    # Filter to items with valid RRP
    valid_comparisons = []
    for row in comparisons:
        store_a_mult = row.get("store_a_qty_multiplier", 1.0) or 1.0
        store_b_mult = row.get("store_b_qty_multiplier", 1.0) or 1.0

        rrp = calculate_rrp(
            row["store_a_converted_cents"],
            row["store_b_converted_cents"],
            store_a_mult,
            store_b_mult,
            store_a_quality=row.get("store_a_match_quality", "none"),
            store_b_quality=row.get("store_b_match_quality", "none"),
            quality_ranking=settings.quality_ranking,
        )
        if rrp is not None:
            valid_comparisons.append((row, rrp))

    if not valid_comparisons:
        return []

    # Batch fetch old RRP values from database
    item_ids = [row["id"] for row, _ in valid_comparisons]
    old_rrps = fetch_current_rrp(item_ids)

    updates = []
    for row, rrp in valid_comparisons:
        quality = best_quality(
            row["store_a_match_quality"], row["store_b_match_quality"],
            quality_ranking=settings.quality_ranking,
        )

        # Read multipliers
        store_a_mult = row.get("store_a_qty_multiplier", 1.0) or 1.0
        store_b_mult = row.get("store_b_qty_multiplier", 1.0) or 1.0

        # Calculate adjusted prices
        store_a_raw = row.get("store_a_converted_cents")
        store_b_raw = row.get("store_b_converted_cents")
        adjusted_store_a = round(store_a_raw * store_a_mult) if store_a_raw is not None else None
        adjusted_store_b = round(store_b_raw * store_b_mult) if store_b_raw is not None else None

        # Build conversion descriptions
        our_weight = row.get("our_weight_g")

        store_a_desc = build_conversion_desc(
            row.get("store_a_unit_price"),
            row.get("store_a_converted_cents"),
            store_a_mult,
            our_weight,
        )
        store_b_desc = build_conversion_desc(
            row.get("store_b_unit_price"),
            row.get("store_b_converted_cents"),
            store_b_mult,
            our_weight,
        )

        # Combine into single string
        parts = []
        if store_a_desc:
            parts.append(f"A: {store_a_desc}")
        if store_b_desc:
            parts.append(f"B: {store_b_desc}")
        conversion_desc = " | ".join(parts) if parts else None

        updates.append(
            UpdateRow(
                id=row["id"],
                name=row["db_name"],
                current_price=row["our_price_cents"],
                new_rrp=rrp,
                store_a_price=adjusted_store_a,
                store_b_price=adjusted_store_b,
                quality=quality,
                old_rrp=old_rrps.get(row["id"]),
                store_a_name=row.get("store_a_name"),
                store_b_name=row.get("store_b_name"),
                conversion_desc=conversion_desc,
                selected=(quality in settings.auto_approve_qualities),
            )
        )

    return updates


def run_approval(state: WizardState) -> bool:
    """
    Run the approval step using existing TUI.

    Returns:
        True if items were approved, False if cancelled.
    """
    if state.comparison_csv_path is None:
        logger.error("No comparison CSV available")
        return False

    # Parse comparison results
    comparisons = parse_comparison_csv(state.comparison_csv_path)
    logger.info(f"Loaded {len(comparisons)} comparison results")

    # Build update rows
    updates = build_updates(comparisons)
    updates.sort(key=lambda u: u.name.lower())
    logger.info(f"Found {len(updates)} items with valid competitor prices")

    if not updates:
        logger.info("No updates to apply")
        state.updates = []
        state.approved = []
        return True

    state.updates = updates

    # Show approval TUI
    approved = show_approval_tui(updates)

    if approved is None:
        logger.info("Cancelled by user")
        return False

    state.approved = approved
    logger.info(f"Approved {len(approved)} items for update")

    return True
