"""
TUI (Text User Interface) for approving price updates.
Uses the rich library for terminal UI.
"""

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from scraper.wizard.components.help_content import HelpEntry

from scraper.stores.store_config import STORE_A_NAME, STORE_B_NAME

# Column names for help navigation
COLUMN_NAMES = [
    "Checkbox",
    "Item",
    "Price",
    "Old RRP",
    "New RRP",
    "Δ RRP",
    "OldDif",
    "NewDif",
    "Δ Dif",
    STORE_A_NAME[:4],
    STORE_B_NAME[:4],
    "Qual",
    "RRP Name",
    "Conv",
]

# Help content for each column (lazily initialized to avoid circular imports)
_COLUMN_HELP: "dict[int, HelpEntry] | None" = None


def _get_column_help() -> "dict[int, HelpEntry]":
    """Get column help dict, initializing lazily to avoid circular imports."""
    global _COLUMN_HELP
    if _COLUMN_HELP is None:
        from scraper.wizard.components.help_content import HelpEntry, HelpTip
        _COLUMN_HELP = {
            0: HelpEntry([HelpTip(
                "Toggle selection with Space. Green [X] means the item is selected "
                "for database update. Unselected items will be skipped."
            )]),
            1: HelpEntry([HelpTip(
                "Product name from your database. The reverse-highlighted row shows "
                "your cursor position. Use Up/Down arrows to navigate between items."
            )]),
            2: HelpEntry([HelpTip(
                "Your current selling price. Green = cheaper than RRP (good margin). "
                "Red = more expensive than RRP (poor competitiveness). Blue = equal to RRP."
            )]),
            3: HelpEntry([HelpTip(
                "The current RRP stored in your database. This is the value that will "
                "be replaced if you approve the update."
            )]),
            4: HelpEntry([HelpTip(
                f"Calculated RRP from {STORE_A_NAME} and {STORE_B_NAME} prices. This is the new recommended "
                "retail price that will be saved to your database."
            )]),
            5: HelpEntry([HelpTip(
                "How the RRP has changed. Cyan = competitors raised their prices (you "
                "become relatively cheaper). Orange = competitors lowered prices (you "
                "become relatively more expensive)."
            )]),
            6: HelpEntry([HelpTip(
                "Your margin vs the OLD RRP. Green = you're cheaper than competitors. "
                "Red = you're more expensive. This shows your position before the update."
            )]),
            7: HelpEntry([HelpTip(
                "Your margin vs the NEW RRP. Green = you're cheaper than competitors. "
                "Red = you're more expensive. This shows your position after the update."
            )]),
            8: HelpEntry([HelpTip(
                "Change in your competitiveness. Green = you're better value now (competitors "
                "raised prices). Red = you're worse value now (competitors lowered prices). "
                "Calculated as NewDif minus OldDif."
            )]),
            9: HelpEntry([HelpTip(
                f"{STORE_A_NAME} price, converted to match your unit size if needed. A dash (-) means "
                f"no match was found at {STORE_A_NAME}."
            )]),
            10: HelpEntry([HelpTip(
                f"{STORE_B_NAME} price, converted to match your unit size if needed. A dash (-) "
                f"means no match was found at {STORE_B_NAME}."
            )]),
            11: HelpEntry([HelpTip(
                "Match quality rating. Green 'good' = high confidence match. Yellow 'ok' = "
                "reasonable match with some uncertainty. Red 'poor' = low confidence, review "
                "carefully before accepting."
            )]),
            12: HelpEntry([HelpTip(
                "Product name from whichever store set the RRP (highest price)."
            )]),
            13: HelpEntry([HelpTip(
                "Shows the conversion math used to calculate competitor prices. "
                "For example: '$5.90/kg x 250g = $1.48'. Scrollable with Left/Right arrows. "
                "Ellipsis (…) indicates more text in that direction."
            )]),
        }
    return _COLUMN_HELP


@dataclass
class UpdateRow:
    """A row in the update approval list."""
    id: int
    name: str
    current_price: int          # Our price (not changing)
    new_rrp: int                # Calculated RRP from competitors
    store_a_price: int | None
    store_b_price: int | None
    quality: Literal["good", "ok", "poor"]
    old_rrp: int | None = None  # Current RRP from DB
    store_a_name: str | None = None      # Name of matched Store A product
    store_b_name: str | None = None # Name of matched Store B product
    conversion_desc: str | None = None # Combined conversion description
    selected: bool = True


def format_price(cents: int | None) -> str:
    """Format cents as dollars."""
    if cents is None:
        return "-"
    return f"${cents / 100:.2f}"


def format_diff(current: int, new: int) -> str:
    """Format price difference as percentage (how new compares to current)."""
    if current == 0:
        return "N/A" if new == 0 else "+∞"
    diff_pct = ((new - current) / current) * 100
    return f"{diff_pct:+.0f}%"


def format_our_vs_rrp(our_price: int, rrp: int | None) -> str:
    """Format how our price compares to RRP. Negative = we're cheaper."""
    if rrp is None or rrp == 0:
        return "-"
    diff_pct = ((our_price - rrp) / rrp) * 100
    return f"{diff_pct:+.0f}%"


def get_quality_style(quality: str) -> str:
    """Get rich style for quality indicator."""
    return {
        "good": "green",
        "ok": "yellow",
        "poor": "red",
    }.get(quality, "white")


def truncate_with_scroll(text: str, width: int, offset: int) -> str:
    """Apply horizontal scroll with … indicators."""
    if not text or width <= 0:
        return "-"
    if len(text) <= width:
        return text  # fits entirely
    # Clamp offset
    max_offset = max(0, len(text) - width + 1)  # +1 for trailing …
    offset = min(offset, max_offset)
    if offset == 0:
        # At start: show beginning + …
        return text[:width - 1] + "…"
    elif offset >= max_offset:
        # At end: … + show ending
        return "…" + text[-(width - 1):]
    else:
        # Middle: … + middle + …
        visible = width - 2  # room for both …
        return "…" + text[offset:offset + visible] + "…"


def _color_competitor_price(price: int | None, current_price: int) -> str:
    """Color a competitor price relative to our price (cyan=higher, orange1=lower)."""
    price_str = format_price(price)
    if price is not None:
        if price > current_price:
            price_str = f"[cyan]{price_str}[/cyan]"
        elif price < current_price:
            price_str = f"[orange1]{price_str}[/orange1]"
    return price_str


def build_table(updates: list[UpdateRow], cursor: int, page_start: int, page_size: int, console_width: int = 120, desc_scroll: int = 0, highlight_column: int | None = None) -> Table:
    """Build the display table for current page."""
    table = Table(
        show_header=True,
        header_style="bold",
        box=None,
        padding=(0, 1),
    )

    table.add_column("", width=3)  # Checkbox
    table.add_column("Item", width=28, no_wrap=True, overflow="ellipsis")
    table.add_column("Price", width=7, justify="right")
    table.add_column("Old RRP", width=7, justify="right")
    table.add_column("New RRP", width=7, justify="right")
    table.add_column("∆ RRP", width=6, justify="right")
    table.add_column("OldDif", width=6, justify="right")
    table.add_column("NewDif", width=6, justify="right")
    table.add_column("∆ Dif", width=6, justify="right")
    table.add_column(STORE_A_NAME[:4], width=7, justify="right")
    table.add_column(STORE_B_NAME[:4], width=7, justify="right")
    table.add_column("Qual", width=4)
    table.add_column("RRP Name", width=28, no_wrap=True, overflow="ellipsis")

    # Dynamic Conv column: fills remaining horizontal space
    # Existing columns: 3+28+7+7+7+6+6+6+6+7+7+4+28 = 122 content + 14 cols * 2 padding = 150
    conv_width = max(10, console_width - 150 - 4)  # 2 for panel borders + 2 for panel padding
    table.add_column("Conv", width=conv_width, no_wrap=True, overflow="ellipsis")

    # Highlight the active help column header
    if highlight_column is not None and 0 <= highlight_column < len(table.columns):
        table.columns[highlight_column].header_style = "bold yellow reverse"

    page_end = min(page_start + page_size, len(updates))

    for i in range(page_start, page_end):
        update = updates[i]

        # Checkbox - [X] for selected, [ ] for unselected
        checkbox = "[bold green][X][/bold green]" if update.selected else "[ ]"

        # Name with cursor indicator
        name = escape(update.name[:28])
        if i == cursor:
            name = f"[reverse]{name}[/reverse]"

        # Our price - color based on comparison to new RRP
        price_str = format_price(update.current_price)
        if update.current_price < update.new_rrp:
            price_str = f"[green]{price_str}[/green]"
        elif update.current_price > update.new_rrp:
            price_str = f"[red]{price_str}[/red]"
        else:
            price_str = f"[blue]{price_str}[/blue]"

        # Old RRP - color vs our price
        old_rrp_str = format_price(update.old_rrp)
        if update.old_rrp is not None:
            if update.old_rrp > update.current_price:
                old_rrp_str = f"[cyan]{old_rrp_str}[/cyan]"
            elif update.old_rrp < update.current_price:
                old_rrp_str = f"[orange1]{old_rrp_str}[/orange1]"

        # Old Diff (how our price compares to old RRP)
        old_diff = format_our_vs_rrp(update.current_price, update.old_rrp)
        if update.old_rrp is not None and update.old_rrp > 0:
            old_diff_val = ((update.current_price - update.old_rrp) / update.old_rrp) * 100
            if old_diff_val < 0:
                old_diff = f"[green]{old_diff}[/green]"
            elif old_diff_val > 0:
                old_diff = f"[red]{old_diff}[/red]"
            else:
                old_diff = f"[blue]{old_diff}[/blue]"

        # New RRP - color vs our price
        new_rrp_str = format_price(update.new_rrp)
        if update.new_rrp > update.current_price:
            new_rrp_str = f"[cyan]{new_rrp_str}[/cyan]"
        elif update.new_rrp < update.current_price:
            new_rrp_str = f"[orange1]{new_rrp_str}[/orange1]"

        # New Diff (how our price compares to new RRP)
        new_diff = format_our_vs_rrp(update.current_price, update.new_rrp)
        new_diff_val = ((update.current_price - update.new_rrp) / update.new_rrp) * 100 if update.new_rrp > 0 else 0
        if new_diff_val < 0:
            new_diff = f"[green]{new_diff}[/green]"
        elif new_diff_val > 0:
            new_diff = f"[red]{new_diff}[/red]"
        else:
            new_diff = f"[blue]{new_diff}[/blue]"

        # Delta RRP (how RRP has changed: new - old)
        if update.old_rrp is not None and update.old_rrp > 0:
            delta_rrp_val = ((update.new_rrp - update.old_rrp) / update.old_rrp) * 100
            delta_rrp = f"{delta_rrp_val:+.0f}%"
            if delta_rrp_val < 0:
                delta_rrp = f"[orange1]{delta_rrp}[/orange1]"  # They've become cheaper
            elif delta_rrp_val > 0:
                delta_rrp = f"[cyan]{delta_rrp}[/cyan]"  # They've become more expensive
            else:
                delta_rrp = f"[white]{delta_rrp}[/white]"
        else:
            delta_rrp = "-"

        # Delta Diff (how our competitiveness has changed)
        # Negative = we're better value vs last time
        if update.old_rrp is not None and update.old_rrp > 0:
            old_comp = ((update.current_price - update.old_rrp) / update.old_rrp) * 100
            new_comp = ((update.current_price - update.new_rrp) / update.new_rrp) * 100 if update.new_rrp > 0 else 0
            delta_diff_val = new_comp - old_comp
            delta_diff = f"{delta_diff_val:+.0f}%"
            if delta_diff_val < 0:
                delta_diff = f"[green]{delta_diff}[/green]"  # We're better value now
            elif delta_diff_val > 0:
                delta_diff = f"[red]{delta_diff}[/red]"  # We're worse value now
            else:
                delta_diff = f"[blue]{delta_diff}[/blue]"
        else:
            delta_diff = "-"

        # Quality with color
        quality_style = get_quality_style(update.quality)
        quality = f"[{quality_style}]{update.quality[:4]}[/{quality_style}]"

        # RRP Name - show name from whichever store provided the RRP (max price)
        store_a_p = update.store_a_price or 0
        store_b_p = update.store_b_price or 0
        if store_a_p >= store_b_p and update.store_a_name:
            rrp_name = update.store_a_name[:28]
        elif update.store_b_name:
            rrp_name = update.store_b_name[:28]
        else:
            rrp_name = "-"

        # Conv column - scroll only applies to cursor row
        row_scroll = desc_scroll if i == cursor else 0
        if update.conversion_desc:
            conv_text = truncate_with_scroll(update.conversion_desc, conv_width, row_scroll)
            conv_cell = f"[magenta]{escape(conv_text)}[/magenta]"
        else:
            conv_cell = "-"

        row = [
            checkbox,
            name,
            price_str,
            old_rrp_str,
            new_rrp_str,
            delta_rrp,
            old_diff,
            new_diff,
            delta_diff,
            _color_competitor_price(update.store_a_price, update.current_price),
            _color_competitor_price(update.store_b_price, update.current_price),
            quality,
            rrp_name,
            conv_cell,
        ]

        table.add_row(*row, style="underline" if i == cursor else "")

    return table


def build_display(
    updates: list[UpdateRow],
    cursor: int,
    page_start: int,
    page_size: int,
    console_width: int = 120,
    help_mode: bool = False,
    help_column: int = 0,
    help_page: int = 0,
    desc_scroll: int = 0,
) -> Panel:
    """Build the full display panel."""
    selected_count = sum(1 for u in updates if u.selected)
    total_count = len(updates)

    # Header
    header = Text()
    header.append("Price Comparison Results", style="bold")
    header.append(f" - {total_count} items with matches\n", style="dim")
    header.append("Up/Down", style="bold cyan")
    header.append(": Navigate  ", style="dim")
    header.append("Space", style="bold cyan")
    header.append(": Toggle  ", style="dim")
    header.append("G", style="bold cyan")
    header.append(": Good  ", style="dim")
    header.append("O", style="bold cyan")
    header.append(": Ok+  ", style="dim")
    header.append("A", style="bold cyan")
    header.append(": All  ", style="dim")
    header.append("N", style="bold cyan")
    header.append(": None  ", style="dim")
    header.append("H", style="bold cyan")
    header.append(": Help  ", style="dim")
    header.append("←/→", style="bold cyan")
    header.append(": Scroll conv  ", style="dim")
    header.append("Enter", style="bold cyan")
    header.append(": Confirm  ", style="dim")
    header.append("Q", style="bold cyan")
    header.append(": Quit", style="dim")

    # Table
    table = build_table(
        updates, cursor, page_start, page_size, console_width, desc_scroll,
        highlight_column=help_column if help_mode else None,
    )

    # Footer
    page_num = (page_start // page_size) + 1
    total_pages = (len(updates) + page_size - 1) // page_size
    footer = Text()
    footer.append(f"\nSelected: {selected_count}/{total_count}", style="bold")
    footer.append(f"  |  Page {page_num}/{total_pages}", style="dim")
    footer.append("  |  Press Enter to update selected items", style="dim")

    # Combine
    content = Text()
    content.append_text(header)
    content.append("\n")

    # Add help box if in help mode
    column_help = _get_column_help()
    if help_mode and help_column in column_help:
        from scraper.wizard.components.help_box import build_help_box
        help_entry = column_help[help_column]
        column_name = COLUMN_NAMES[help_column] if help_column < len(COLUMN_NAMES) else f"Column {help_column}"
        indicator = f"Column: {column_name} ({help_column + 1}/{len(COLUMN_NAMES)})"
        help_box = build_help_box(help_entry, help_page, indicator)
        panel_content = Group(table, Text(""), help_box)
    else:
        panel_content = table

    return Panel(
        panel_content,
        title=str(header),
        subtitle=str(footer),
        border_style="blue",
    )


def get_key() -> str:
    """Get a single keypress (cross-platform)."""
    if sys.platform == "win32":
        import msvcrt
        key = msvcrt.getch()
        if key == b"\xe0" or key == b"\x00":  # Arrow key prefixes on Windows
            key = msvcrt.getch()
            if key == b"H":
                return "up"
            elif key == b"P":
                return "down"
            elif key == b"K":
                return "left"
            elif key == b"M":
                return "right"
        elif key == b"\r":
            return "enter"
        elif key == b" ":
            return "space"
        elif key == b"q" or key == b"Q":
            return "q"
        elif key == b"a" or key == b"A":
            return "a"
        elif key == b"n" or key == b"N":
            return "n"
        elif key == b"g" or key == b"G":
            return "g"
        elif key == b"o" or key == b"O":
            return "o"
        elif key == b"h":
            return "h"
        return key.decode("utf-8", errors="ignore")
    else:
        import tty
        import termios
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":  # Escape sequence
                import select
                readable, _, _ = select.select([sys.stdin], [], [], 0.05)
                if not readable:
                    return "\x1b"  # Lone Escape key
                ch2 = sys.stdin.read(1)
                if ch2 == "[":
                    readable2, _, _ = select.select([sys.stdin], [], [], 0.05)
                    if not readable2:
                        return "\x1b"
                    ch3 = sys.stdin.read(1)
                    if ch3 == "A":
                        return "up"
                    elif ch3 == "B":
                        return "down"
                    elif ch3 == "C":
                        return "right"
                    elif ch3 == "D":
                        return "left"
            elif ch == "\r" or ch == "\n":
                return "enter"
            elif ch == " ":
                return "space"
            elif ch.lower() == "q":
                return "q"
            elif ch.lower() == "a":
                return "a"
            elif ch.lower() == "n":
                return "n"
            elif ch.lower() == "g":
                return "g"
            elif ch.lower() == "o":
                return "o"
            elif ch.lower() == "h":
                return "h"
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def show_approval_tui(updates: list[UpdateRow]) -> list[UpdateRow] | None:
    """
    Show the approval TUI and return selected items.

    Args:
        updates: List of UpdateRow objects to display

    Returns:
        List of selected UpdateRow objects, or None if cancelled
    """
    if not updates:
        return []

    from scraper.wizard.settings import SettingsManager

    console = Console()
    cursor = 0

    # Load settings for max page size
    settings = SettingsManager().load()
    max_page_size = settings.page_size or 30

    # Use available vertical space (less reserved for header/footer)
    page_size = max(1, min(max_page_size, console.height - 8))
    page_start = 0

    # Help mode state
    help_mode = False
    help_column = 0
    help_page = 0

    # Conversion column scroll state
    desc_scroll = 0

    # Use Rich Live for flicker-free updates
    with Live(console=console, refresh_per_second=30, screen=True) as live:
        while True:
            # Adjust page if cursor moves out of view
            if cursor < page_start:
                page_start = cursor
            elif cursor >= page_start + page_size:
                page_start = cursor - page_size + 1

            # Build and display
            panel = build_display(
                updates, cursor, page_start, page_size, console.width,
                help_mode, help_column, help_page, desc_scroll
            )
            live.update(panel)

            # Get input
            key = get_key()

            if help_mode:
                # In help mode: ←/→ navigates columns, H/any nav key exits
                if key == "left":
                    help_column = (help_column - 1) % len(COLUMN_NAMES)
                    help_page = 0
                elif key == "right":
                    help_column = (help_column + 1) % len(COLUMN_NAMES)
                    help_page = 0
                elif key == "h":
                    help_mode = False
                elif key in ("up", "down", "space", "enter", "q"):
                    # Any navigation key exits help mode
                    help_mode = False
            else:
                # Normal mode
                if key == "up":
                    cursor = max(0, cursor - 1)
                    desc_scroll = 0
                elif key == "down":
                    cursor = min(len(updates) - 1, cursor + 1)
                    desc_scroll = 0
                elif key == "space":
                    updates[cursor].selected = not updates[cursor].selected
                    cursor = min(len(updates) - 1, cursor + 1)
                elif key == "a":
                    for u in updates:
                        u.selected = True
                elif key == "n":
                    for u in updates:
                        u.selected = False
                elif key == "g":
                    # Select only good matches
                    for u in updates:
                        u.selected = (u.quality == "good")
                elif key == "o":
                    # Select good and ok matches
                    auto_quals = settings.auto_approve_qualities
                    for u in updates:
                        u.selected = (u.quality in auto_quals)
                elif key == "left":
                    desc_scroll = max(0, desc_scroll - 5)
                elif key == "right":
                    desc_scroll += 5
                elif key == "h":
                    help_mode = True
                    help_column = 0
                    help_page = 0
                elif key == "enter":
                    selected = [u for u in updates if u.selected]
                    return selected
                elif key == "q":
                    return None


def show_summary(updates: list[UpdateRow], executed: bool) -> None:
    """Show a summary of what was/would be updated."""
    console = Console()

    table = Table(title="Update Summary", show_header=True)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")

    selected = [u for u in updates if u.selected]
    good = sum(1 for u in selected if u.quality == "good")
    ok = sum(1 for u in selected if u.quality == "ok")
    poor = sum(1 for u in selected if u.quality == "poor")

    table.add_row("Total items", str(len(updates)))
    table.add_row("Selected for update", str(len(selected)))
    table.add_row("Good matches", str(good))
    table.add_row("OK matches", str(ok))
    table.add_row("Poor matches", str(poor))

    if executed:
        table.add_row("Status", "[green]Updated[/green]")
    else:
        table.add_row("Status", "[yellow]Dry run (no changes)[/yellow]")

    console.print(table)
