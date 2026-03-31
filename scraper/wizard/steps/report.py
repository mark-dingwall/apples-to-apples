"""Step 6: Final report and execution."""

import csv
import json
import logging
from datetime import datetime
from pathlib import Path
from statistics import median

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from scraper.db import execute_updates
from scraper.stores.store_config import STORE_A_NAME, STORE_B_NAME, STORE_A_COL, STORE_B_COL
from scraper.tui import UpdateRow
from scraper.utils.swot import build_swot, build_swot_llm
from scraper.wizard.components.menu import get_key
from scraper.wizard.settings import Settings
from scraper.wizard.state import WizardState

logger = logging.getLogger(__name__)


def format_price(cents: int | None) -> str:
    """Format cents as dollars."""
    if cents is None:
        return "-"
    return f"${cents / 100:.2f}"


def format_diff_pct(
    old: int, new: int, context: str = "our_vs_rrp"
) -> tuple[str, str]:
    """
    Format price difference as percentage with context-aware coloring.

    Args:
        old: Original price in cents
        new: New price in cents
        context: Color scheme to use:
            - "our_vs_rrp": Our price vs RRP (green = our price lower, red = higher)
            - "rrp_change": Old RRP vs new RRP (cyan = RRP increased, orange = decreased)

    Returns:
        (formatted string, style for rich)
    """
    if old == 0:
        return "+inf%", "red"

    diff_pct = ((new - old) / old) * 100

    if context == "rrp_change":
        # RRP changes: cyan = increased (good for us), orange = decreased (bad)
        if diff_pct > 0:
            return f"+{diff_pct:.1f}%", "cyan"
        elif diff_pct < 0:
            return f"{diff_pct:.1f}%", "orange1"
        else:
            return "0.0%", "dim"
    else:
        # Our price vs RRP: negative = we're cheaper (green), positive = more expensive (red)
        if diff_pct > 0:
            return f"+{diff_pct:.1f}%", "red"
        elif diff_pct < 0:
            return f"{diff_pct:.1f}%", "green"
        else:
            return "0.0%", "dim"


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


def _pct_str(count: int, total: int) -> str:
    """Format 'N (X.X%)' safely."""
    if total == 0:
        return f"{count} (0.0%)"
    return f"{count} ({count / total * 100:.1f}%)"


def _load_csv_data(csv_path: Path) -> list[dict]:
    """Read comparison CSV into list of dicts."""
    try:
        with open(csv_path, encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception as e:
        logger.warning(f"Could not read comparison CSV: {e}")
        return []


def _safe_int(val: str | None) -> int | None:
    """Parse a string to int, returning None on failure."""
    if not val or val == "-":
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return None


def _compute_price_positioning(updates: list[UpdateRow]) -> dict:
    """Compute below/above/equal RRP counts and margin stats."""
    below = above = equal = 0
    margins = []
    for u in updates:
        if u.new_rrp == 0:
            continue
        if u.current_price < u.new_rrp:
            below += 1
        elif u.current_price > u.new_rrp:
            above += 1
        else:
            equal += 1
        margins.append((u.current_price - u.new_rrp) / u.new_rrp * 100)
    total = below + above + equal
    avg_margin = sum(margins) / len(margins) if margins else 0
    med_margin = median(margins) if margins else 0
    return {
        "below": below, "above": above, "equal": equal,
        "total": total, "avg_margin": avg_margin, "med_margin": med_margin,
    }


def _compute_store_coverage(csv_rows: list[dict]) -> dict:
    """Compute per-store match rates and quality from CSV data."""
    total = len(csv_rows)
    store_a_matches = store_a_good = store_a_ok = store_a_poor = 0
    store_b_matches = store_b_good = store_b_ok = store_b_poor = 0
    both = neither = single = 0

    for row in csv_rows:
        has_store_a = bool(row.get(f"{STORE_A_COL}_converted_cents") and row[f"{STORE_A_COL}_converted_cents"] != "-")
        has_store_b = bool(row.get(f"{STORE_B_COL}_converted_cents") and row[f"{STORE_B_COL}_converted_cents"] != "-")

        if has_store_a:
            store_a_matches += 1
            q = (row.get(f"{STORE_A_COL}_match_quality") or "").lower()
            if q == "good":
                store_a_good += 1
            elif q == "ok":
                store_a_ok += 1
            elif q == "poor":
                store_a_poor += 1

        if has_store_b:
            store_b_matches += 1
            q = (row.get(f"{STORE_B_COL}_match_quality") or "").lower()
            if q == "good":
                store_b_good += 1
            elif q == "ok":
                store_b_ok += 1
            elif q == "poor":
                store_b_poor += 1

        if has_store_a and has_store_b:
            both += 1
        elif not has_store_a and not has_store_b:
            neither += 1
        else:
            single += 1

    return {
        "total": total,
        "store_a_matches": store_a_matches, "store_a_good": store_a_good,
        "store_a_ok": store_a_ok, "store_a_poor": store_a_poor,
        "store_b_matches": store_b_matches, "store_b_good": store_b_good,
        "store_b_ok": store_b_ok, "store_b_poor": store_b_poor,
        "both": both, "neither": neither, "single": single,
    }


def _compute_cheapest(updates: list[UpdateRow]) -> dict:
    """Compute who's cheapest breakdown."""
    us = store_a = store_b = 0
    for u in updates:
        prices = {"us": u.current_price}
        if u.store_a_price is not None:
            prices["store_a"] = u.store_a_price
        if u.store_b_price is not None:
            prices["store_b"] = u.store_b_price
        if len(prices) < 2:
            continue
        cheapest = min(prices, key=lambda k: prices[k])
        if cheapest == "us":
            us += 1
        elif cheapest == "store_a":
            store_a += 1
        else:
            store_b += 1
    total = us + store_a + store_b
    return {"us": us, "store_a": store_a, "store_b": store_b, "total": total}


def _compute_rrp_movement(updates: list[UpdateRow]) -> dict:
    """Compute RRP change direction counts."""
    increased = decreased = unchanged = 0
    inc_amounts = []
    dec_amounts = []
    for u in updates:
        if u.old_rrp is None or u.old_rrp == 0:
            continue
        if u.new_rrp > u.old_rrp:
            increased += 1
            inc_amounts.append(u.new_rrp - u.old_rrp)
        elif u.new_rrp < u.old_rrp:
            decreased += 1
            dec_amounts.append(u.old_rrp - u.new_rrp)
        else:
            unchanged += 1
    total = increased + decreased + unchanged
    avg_inc = sum(inc_amounts) / len(inc_amounts) if inc_amounts else 0
    avg_dec = sum(dec_amounts) / len(dec_amounts) if dec_amounts else 0
    return {
        "increased": increased, "decreased": decreased, "unchanged": unchanged,
        "total": total, "avg_inc": avg_inc, "avg_dec": avg_dec,
    }


def _compute_outliers(updates: list[UpdateRow]) -> dict:
    """Compute 4 sorted top-5 outlier lists."""
    above_rrp = []
    below_rrp = []
    rrp_increases = []
    rrp_decreases = []

    for u in updates:
        if u.new_rrp > 0:
            diff = u.current_price - u.new_rrp
            pct = diff / u.new_rrp * 100
            if diff > 0:
                above_rrp.append((u.name, u.current_price, u.new_rrp, diff, pct))
            elif diff < 0:
                below_rrp.append((u.name, u.current_price, u.new_rrp, abs(diff), abs(pct)))

        if u.old_rrp is not None and u.old_rrp > 0:
            change = u.new_rrp - u.old_rrp
            change_pct = change / u.old_rrp * 100
            if change > 0:
                rrp_increases.append((u.name, u.old_rrp, u.new_rrp, change, change_pct))
            elif change < 0:
                rrp_decreases.append((u.name, u.old_rrp, u.new_rrp, abs(change), abs(change_pct)))

    above_rrp.sort(key=lambda x: x[4], reverse=True)
    below_rrp.sort(key=lambda x: x[4], reverse=True)
    rrp_increases.sort(key=lambda x: x[4], reverse=True)
    rrp_decreases.sort(key=lambda x: x[4], reverse=True)

    return {
        "above_rrp": above_rrp[:5],
        "below_rrp": below_rrp[:5],
        "rrp_increases": rrp_increases[:5],
        "rrp_decreases": rrp_decreases[:5],
    }


def _build_concerns(
    positioning: dict, coverage: dict, cheapest: dict, rrp_mov: dict,
    csv_rows: list[dict],
) -> list[str]:
    """Generate threshold-based concern strings."""
    concerns = []

    # >30% of items above RRP
    if positioning["total"] > 0:
        above_pct = positioning["above"] / positioning["total"] * 100
        if above_pct > 30:
            concerns.append(
                f"{above_pct:.0f}% of items are priced above RRP "
                f"({positioning['above']}/{positioning['total']})"
            )

    # >15% poor match quality (combined from CSV)
    if coverage["total"] > 0:
        total_poor = coverage["store_a_poor"] + coverage["store_b_poor"]
        total_matches = coverage["store_a_matches"] + coverage["store_b_matches"]
        if total_matches > 0:
            poor_pct = total_poor / total_matches * 100
            if poor_pct > 15:
                concerns.append(
                    f"{poor_pct:.0f}% of store matches are poor quality "
                    f"({total_poor}/{total_matches})"
                )

    # >20% no store matches
    if coverage["total"] > 0:
        no_match_pct = coverage["neither"] / coverage["total"] * 100
        if no_match_pct > 20:
            concerns.append(
                f"{no_match_pct:.0f}% of items had no store matches "
                f"({coverage['neither']}/{coverage['total']})"
            )

    # We're cheapest on <30% of items
    if cheapest["total"] > 0:
        us_pct = cheapest["us"] / cheapest["total"] * 100
        if us_pct < 30:
            concerns.append(
                f"We are the cheapest on only {us_pct:.0f}% of comparable items "
                f"({cheapest['us']}/{cheapest['total']})"
            )

    # >30% of RRPs decreased
    if rrp_mov["total"] > 0:
        dec_pct = rrp_mov["decreased"] / rrp_mov["total"] * 100
        if dec_pct > 30:
            concerns.append(
                f"{dec_pct:.0f}% of RRPs decreased -- competitors are dropping prices "
                f"({rrp_mov['decreased']}/{rrp_mov['total']})"
            )

    # Many LLM-only conversions
    llm_only = 0
    for row in csv_rows:
        for store in ("store_a", "store_b"):
            method = (row.get(f"{store}_conversion_method") or "").lower()
            if method and "llm" in method and "math" not in method:
                llm_only += 1
    total_conversions = coverage["store_a_matches"] + coverage["store_b_matches"]
    if total_conversions > 0 and llm_only > 0:
        llm_pct = llm_only / total_conversions * 100
        if llm_pct > 30:
            concerns.append(
                f"{llm_pct:.0f}% of conversions are LLM-only (no math guardrail) "
                f"({llm_only}/{total_conversions})"
            )

    return concerns


def _build_swot(
    positioning: dict, coverage: dict, cheapest: dict, rrp_mov: dict,
) -> dict[str, list[str]]:
    """Build SWOT quadrant content (rule-based). Delegates to scraper.utils.swot."""
    return build_swot(positioning, coverage, cheapest, rrp_mov)


def _build_swot_llm(
    positioning: dict, coverage: dict, cheapest: dict, rrp_mov: dict,
    concerns: list[str], outliers: dict,
) -> dict[str, list[str]] | None:
    """Build SWOT using LLM. Returns None on failure (caller should fall back).
    Delegates to scraper.utils.swot."""
    return build_swot_llm(positioning, coverage, cheapest, rrp_mov, concerns, outliers)


def _render_swot_quadrant(
    title: str, items: list[str], border_style: str,
) -> Panel:
    """Render one SWOT quadrant as a Rich Panel."""
    if items:
        body = "\n".join(f"  o {item}" for item in items)
    else:
        body = "[dim]  No significant factors identified.[/dim]"
    return Panel(body, title=f"[bold]{title}[/bold]", border_style=border_style, expand=True)


def _render_expanded_report(
    console: Console, state: WizardState,
) -> None:
    """Render expanded analysis sections (A through G)."""
    updates = state.updates  # All candidates, not just approved

    if not updates:
        return

    # Load CSV data
    csv_rows = []
    if state.comparison_csv_path and state.comparison_csv_path.exists():
        csv_rows = _load_csv_data(state.comparison_csv_path)

    # Compute all stats
    positioning = _compute_price_positioning(updates)
    coverage = _compute_store_coverage(csv_rows) if csv_rows else {
        "total": 0, "store_a_matches": 0, "store_a_good": 0, "store_a_ok": 0,
        "store_a_poor": 0, "store_b_matches": 0, "store_b_good": 0, "store_b_ok": 0,
        "store_b_poor": 0, "both": 0, "neither": 0, "single": 0,
    }
    cheapest = _compute_cheapest(updates)
    rrp_mov = _compute_rrp_movement(updates)
    outliers = _compute_outliers(updates)

    # === Section A: Comparison Statistics ===
    if positioning["total"] > 0:
        pos_table = Table(title="Price Positioning vs RRP", show_header=False)
        pos_table.add_column("Metric", style="bold", width=30)
        pos_table.add_column("Value", justify="right")

        pos_table.add_row(
            "Below RRP (cheaper)",
            f"[green]{_pct_str(positioning['below'], positioning['total'])}[/green]",
        )
        pos_table.add_row(
            "Above RRP (more expensive)",
            f"[red]{_pct_str(positioning['above'], positioning['total'])}[/red]",
        )
        pos_table.add_row(
            "Equal to RRP",
            f"[blue]{_pct_str(positioning['equal'], positioning['total'])}[/blue]",
        )

        avg_style = "green" if positioning["avg_margin"] < 0 else "red"
        pos_table.add_row(
            "Avg margin vs RRP",
            f"[{avg_style}]{positioning['avg_margin']:+.1f}%[/{avg_style}]",
        )
        med_style = "green" if positioning["med_margin"] < 0 else "red"
        pos_table.add_row(
            "Median margin vs RRP",
            f"[{med_style}]{positioning['med_margin']:+.1f}%[/{med_style}]",
        )

        console.print()
        console.print(pos_table)

    # === Section B: Store Coverage ===
    if coverage["total"] > 0:
        cov_table = Table(title="Store Coverage")
        cov_table.add_column("Store", style="bold", width=20)
        cov_table.add_column("Matches", justify="right")
        cov_table.add_column("Good", justify="right")
        cov_table.add_column("OK", justify="right")
        cov_table.add_column("Poor", justify="right")

        cov_table.add_row(
            STORE_A_NAME,
            _pct_str(coverage["store_a_matches"], coverage["total"]),
            f"[green]{coverage['store_a_good']}[/green]",
            f"[yellow]{coverage['store_a_ok']}[/yellow]",
            f"[red]{coverage['store_a_poor']}[/red]",
        )
        cov_table.add_row(
            STORE_B_NAME,
            _pct_str(coverage["store_b_matches"], coverage["total"]),
            f"[green]{coverage['store_b_good']}[/green]",
            f"[yellow]{coverage['store_b_ok']}[/yellow]",
            f"[red]{coverage['store_b_poor']}[/red]",
        )

        cov_table.add_section()
        cov_table.add_row("Both stores", _pct_str(coverage["both"], coverage["total"]), "", "", "")
        cov_table.add_row("Neither store", _pct_str(coverage["neither"], coverage["total"]), "", "", "")
        cov_table.add_row("Single store only", _pct_str(coverage["single"], coverage["total"]), "", "", "")

        console.print()
        console.print(cov_table)

    # === Section C: Cheapest Comparison ===
    if cheapest["total"] > 0:
        cheap_table = Table(title="Who's Cheapest?", show_header=False)
        cheap_table.add_column("Who", style="bold", width=20)
        cheap_table.add_column("Count", justify="right")

        cheap_table.add_row(
            "Us",
            f"[green]{_pct_str(cheapest['us'], cheapest['total'])}[/green]",
        )
        cheap_table.add_row(
            STORE_A_NAME,
            f"[red]{_pct_str(cheapest['store_a'], cheapest['total'])}[/red]",
        )
        cheap_table.add_row(
            STORE_B_NAME,
            f"[red]{_pct_str(cheapest['store_b'], cheapest['total'])}[/red]",
        )

        console.print()
        console.print(cheap_table)

    # === Section D: RRP Movement Summary ===
    if rrp_mov["total"] > 0:
        mov_table = Table(title="RRP Movement Summary", show_header=False)
        mov_table.add_column("Metric", style="bold", width=20)
        mov_table.add_column("Value", justify="right")

        mov_table.add_row(
            "RRP increased",
            f"[cyan]{_pct_str(rrp_mov['increased'], rrp_mov['total'])}[/cyan]",
        )
        if rrp_mov["avg_inc"] > 0:
            mov_table.add_row(
                "  Avg increase",
                f"[cyan]${rrp_mov['avg_inc'] / 100:.2f}[/cyan]",
            )
        mov_table.add_row(
            "RRP decreased",
            f"[orange1]{_pct_str(rrp_mov['decreased'], rrp_mov['total'])}[/orange1]",
        )
        if rrp_mov["avg_dec"] > 0:
            mov_table.add_row(
                "  Avg decrease",
                f"[orange1]${rrp_mov['avg_dec'] / 100:.2f}[/orange1]",
            )
        mov_table.add_row(
            "Unchanged",
            f"[dim]{_pct_str(rrp_mov['unchanged'], rrp_mov['total'])}[/dim]",
        )

        console.print()
        console.print(mov_table)

    # === Section E: Outliers ===
    if outliers["above_rrp"]:
        t = Table(title="Most Expensive vs RRP (Top 5)")
        t.add_column("Item", width=30, no_wrap=True, overflow="ellipsis")
        t.add_column("Ours", justify="right")
        t.add_column("RRP", justify="right")
        t.add_column("Diff", justify="right")
        for name, ours, rrp, diff_c, pct in outliers["above_rrp"]:
            t.add_row(
                name[:30], format_price(ours), format_price(rrp),
                f"[red]+{pct:.0f}%[/red]",
            )
        console.print()
        console.print(t)

    if outliers["below_rrp"]:
        t = Table(title="Best Value vs RRP (Top 5)")
        t.add_column("Item", width=30, no_wrap=True, overflow="ellipsis")
        t.add_column("Ours", justify="right")
        t.add_column("RRP", justify="right")
        t.add_column("Diff", justify="right")
        for name, ours, rrp, diff_c, pct in outliers["below_rrp"]:
            t.add_row(
                name[:30], format_price(ours), format_price(rrp),
                f"[green]-{pct:.0f}%[/green]",
            )
        console.print()
        console.print(t)

    if outliers["rrp_increases"]:
        t = Table(title="Biggest RRP Increases (Top 5)")
        t.add_column("Item", width=30, no_wrap=True, overflow="ellipsis")
        t.add_column("Old RRP", justify="right")
        t.add_column("New RRP", justify="right")
        t.add_column("Change", justify="right")
        for name, old, new, diff_c, pct in outliers["rrp_increases"]:
            t.add_row(
                name[:30], format_price(old), format_price(new),
                f"[cyan]+{pct:.0f}%[/cyan]",
            )
        console.print()
        console.print(t)

    if outliers["rrp_decreases"]:
        t = Table(title="Biggest RRP Decreases (Top 5)")
        t.add_column("Item", width=30, no_wrap=True, overflow="ellipsis")
        t.add_column("Old RRP", justify="right")
        t.add_column("New RRP", justify="right")
        t.add_column("Change", justify="right")
        for name, old, new, diff_c, pct in outliers["rrp_decreases"]:
            t.add_row(
                name[:30], format_price(old), format_price(new),
                f"[orange1]-{pct:.0f}%[/orange1]",
            )
        console.print()
        console.print(t)

    # === Section F: Concerns ===
    concerns = _build_concerns(positioning, coverage, cheapest, rrp_mov, csv_rows)
    if concerns:
        body = "\n".join(f"  o {c}" for c in concerns)
        console.print()
        console.print(Panel(
            body,
            title="[bold]Concerns & Notable Patterns[/bold]",
            border_style="yellow",
        ))
    else:
        console.print()
        console.print(Panel(
            "[green]  No significant concerns identified.[/green]",
            title="[bold]Concerns & Notable Patterns[/bold]",
            border_style="yellow",
        ))

    # === Section G: SWOT Analysis ===
    console.print()
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(pulse_style="cyan"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as swot_progress:
        swot_progress.add_task("Generating SWOT analysis (LLM)...", total=None)
        swot = _build_swot_llm(positioning, coverage, cheapest, rrp_mov, concerns, outliers)
    if swot is None:
        swot = _build_swot(positioning, coverage, cheapest, rrp_mov)
    # Stash SWOT data for HTML report (avoids duplicate LLM call)
    state._swot_data = swot

    grid = Table.grid(expand=True)
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)
    grid.add_row(
        _render_swot_quadrant("Strengths", swot["strengths"], "green"),
        _render_swot_quadrant("Weaknesses", swot["weaknesses"], "red"),
    )
    grid.add_row(
        _render_swot_quadrant("Opportunities", swot["opportunities"], "cyan"),
        _render_swot_quadrant("Threats", swot["threats"], "orange1"),
    )
    console.print()
    console.print(Panel(
        grid,
        title="[bold]SWOT Analysis[/bold]",
        border_style="blue",
    ))


def run_report(
    state: WizardState, settings: Settings, output_dir: Path
) -> bool:
    """
    Show final report and optionally execute updates.

    Returns:
        True if completed successfully, False otherwise.
    """
    console = Console()

    # Calculate statistics
    total_updates = len(state.updates)
    selected_count = len(state.approved)
    good_count = sum(1 for u in state.approved if u.quality == "good")
    ok_count = sum(1 for u in state.approved if u.quality == "ok")
    poor_count = sum(1 for u in state.approved if u.quality == "poor")
    skipped_count = total_updates - selected_count

    # Summary table
    summary_table = Table(title="Pipeline Complete", show_header=False)
    summary_table.add_column("Metric", style="bold")
    summary_table.add_column("Value", justify="right")

    summary_table.add_row("Items processed", str(state.item_count))
    summary_table.add_row("Updates available", str(total_updates))
    summary_table.add_row("Updates selected", str(selected_count))
    summary_table.add_row("  Good matches", f"[green]{good_count}[/green]")
    summary_table.add_row("  OK matches", f"[yellow]{ok_count}[/yellow]")
    summary_table.add_row("  Poor matches", f"[red]{poor_count}[/red]")
    summary_table.add_row("Skipped (poor/none)", str(skipped_count))

    console.print()
    console.print(summary_table)

    # Significant price differences table
    threshold = settings.significant_price_diff_pct
    significant_diffs = []

    for update in state.approved:
        if update.current_price == 0 or update.new_rrp == 0:
            continue

        # From our perspective: negative = we're cheaper, positive = we're more expensive
        diff_pct = ((update.current_price - update.new_rrp) / update.new_rrp) * 100

        if abs(diff_pct) >= threshold:
            significant_diffs.append(
                {
                    "name": update.name,
                    "our_price": update.current_price,
                    "rrp": update.new_rrp,
                    "diff_pct": diff_pct,
                }
            )

    if significant_diffs:
        diff_table = Table(title=f"Significant Price Differences (>{threshold:.0f}%)")
        diff_table.add_column("Item", width=35, no_wrap=True, overflow="ellipsis")
        diff_table.add_column("Ours", justify="right")
        diff_table.add_column("RRP", justify="right")
        diff_table.add_column("Diff", justify="right")

        # Sort by absolute difference
        significant_diffs.sort(key=lambda x: abs(x["diff_pct"]), reverse=True)

        for item in significant_diffs[:15]:  # Show top 15
            diff_str, diff_style = format_diff_pct(
                item["rrp"], item["our_price"]
            )

            diff_table.add_row(
                item["name"][:35],
                format_price(item["our_price"]),
                format_price(item["rrp"]),
                f"[{diff_style}]{diff_str}[/{diff_style}]",
            )

        console.print()
        console.print(diff_table)

    # RRP changes table (using old_rrp already loaded during approval step)
    if state.approved:
        rrp_changes = []
        for update in state.approved:
            if update.old_rrp is not None and update.old_rrp > 0 and update.old_rrp != update.new_rrp:
                diff_pct = ((update.new_rrp - update.old_rrp) / update.old_rrp) * 100
                if abs(diff_pct) >= threshold:
                    rrp_changes.append(
                        {
                            "name": update.name,
                            "old_rrp": update.old_rrp,
                            "new_rrp": update.new_rrp,
                            "diff_pct": diff_pct,
                        }
                    )

        if rrp_changes:
            rrp_table = Table(title="Major RRP Changes (vs current DB)")
            rrp_table.add_column("Item", width=35, no_wrap=True, overflow="ellipsis")
            rrp_table.add_column("Old RRP", justify="right")
            rrp_table.add_column("New RRP", justify="right")
            rrp_table.add_column("Change", justify="right")

            rrp_changes.sort(key=lambda x: abs(x["diff_pct"]), reverse=True)

            for item in rrp_changes[:10]:  # Show top 10
                diff_str, diff_style = format_diff_pct(
                    item["old_rrp"], item["new_rrp"], context="rrp_change"
                )

                rrp_table.add_row(
                    item["name"][:35],
                    format_price(item["old_rrp"]),
                    format_price(item["new_rrp"]),
                    f"[{diff_style}]{diff_str}[/{diff_style}]",
                )

            console.print()
            console.print(rrp_table)

    # Expanded analysis sections
    _render_expanded_report(console, state)

    # If no approved items, we're done
    if not state.approved:
        console.print("\n[yellow]No items selected for update.[/yellow]")
        _print_final_summary(console, state, settings, output_dir, 0)
        return True

    # Ask for confirmation
    console.print()
    console.print(
        Panel(
            "[bold]Confirm Database Update[/bold]\n\n"
            f"[dim]{selected_count} items ready to update.[/dim]\n\n"
            "Press [bold cyan]Y[/bold cyan] to execute updates, "
            "or [bold cyan]N[/bold cyan] to exit without changes.",
            border_style="yellow",
        )
    )

    while True:
        key = get_key()
        if key.lower() == "y":
            break
        elif key.lower() == "n" or key == "q":
            console.print("[dim]Exiting without database changes.[/dim]")
            _print_final_summary(console, state, settings, output_dir, 0)
            return True

    # Execute updates
    update_tuples = [(u.new_rrp, u.id) for u in state.approved]
    affected = 0

    try:
        console.print("\n[bold]Executing database updates...[/bold]")
        affected = execute_updates(update_tuples, dry_run=False)
        state.executed = True
        console.print(f"[green]Updated {affected} rows in database.[/green]")

        # Write audit log only after successful execution
        audit_log = output_dir / f"pipeline_audit_{state.run_timestamp}.log"
        write_audit_log(state.approved, audit_log)
        state.audit_log_path = audit_log
        console.print(f"[dim]Audit log: {audit_log}[/dim]")
    except Exception as e:
        console.print(f"[red]Failed to execute updates: {e}[/red]")
        return False

    _print_final_summary(console, state, settings, output_dir, affected)
    return True


def _print_final_summary(
    console: Console, state: WizardState, settings: Settings | None, output_dir: Path, affected: int
) -> None:
    """Print the final execution summary and generated files."""
    console.print()
    console.print("=" * 50)
    console.print("[bold green]PIPELINE COMPLETE[/bold green]")
    console.print("=" * 50)

    # Execution summary table
    exec_table = Table(title="Execution Summary", show_header=False)
    exec_table.add_column("Metric", style="bold")
    exec_table.add_column("Value", justify="right")

    exec_table.add_row("Items processed", str(state.item_count))
    exec_table.add_row("Updates selected", str(len(state.approved)))

    if state.executed:
        exec_table.add_row("Database rows updated", f"[green]{affected}[/green]")
        exec_table.add_row("Status", "[green]Committed[/green]")
    else:
        exec_table.add_row("Database rows updated", "[dim]0[/dim]")
        exec_table.add_row("Status", "[yellow]No changes made[/yellow]")

    console.print()
    console.print(exec_table)

    # Generated files table
    files_table = Table(title="Generated Files")
    files_table.add_column("File", style="cyan")
    files_table.add_column("Description")

    if state.temp_csv_path and state.temp_csv_path.exists():
        files_table.add_row(str(state.temp_csv_path), "Input CSV with search terms")

    if state.results_path and state.results_path.exists():
        files_table.add_row(str(state.results_path), "Scraper results (JSON)")

    if state.comparison_csv_path and state.comparison_csv_path.exists():
        files_table.add_row(str(state.comparison_csv_path), "Price comparison (CSV)")

    if state.audit_log_path and state.audit_log_path.exists():
        files_table.add_row(str(state.audit_log_path), "Audit log")

    # Generate HTML report
    try:
        from scraper.html_report import generate_html_report
        swot_data = getattr(state, "_swot_data", None)
        html_path = generate_html_report(
            updates=state.updates,
            approved=state.approved,
            comparison_csv_path=state.comparison_csv_path,
            offer_id=state.offer_id,
            timestamp=state.run_timestamp,
            output_dir=output_dir,
            settings_threshold=settings.significant_price_diff_pct if settings else 25.0,
            swot_data=swot_data,
        )
        if html_path:
            state.html_report_path = html_path
            files_table.add_row(str(html_path), "HTML report")
    except Exception as e:
        logger.warning(f"Could not generate HTML report: {e}")

    console.print()
    console.print(files_table)

    # Note about cached files
    if state.use_cached_csv or state.use_cached_results:
        cached_items = []
        if state.use_cached_csv:
            cached_items.append("input CSV")
        if state.use_cached_results:
            cached_items.append("scraper results")
        console.print(f"\n[dim]Note: Used cached {' and '.join(cached_items)}[/dim]")
