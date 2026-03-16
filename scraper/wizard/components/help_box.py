"""Reusable help box rendering component."""

from rich.panel import Panel
from rich.text import Text

from scraper.wizard.components.help_content import HelpEntry


def build_help_box(
    help_entry: HelpEntry,
    current_page: int = 0,
    indicator_text: str = "",
) -> Panel:
    """
    Build a help box panel with yellow border.

    Args:
        help_entry: The help entry containing tips
        current_page: Current page index (0-based)
        indicator_text: Optional indicator text (e.g., column name)

    Returns:
        Rich Panel with help content
    """
    tip = help_entry.get_tip(current_page)
    page_count = help_entry.page_count

    content = Text()
    content.append(tip.text, style="white")

    # Build subtitle with pagination if multiple tips
    subtitle_parts = []
    if indicator_text:
        subtitle_parts.append(indicator_text)
    if page_count > 1:
        subtitle_parts.append(f"Tip {current_page + 1}/{page_count}")
        subtitle_parts.append("Left/Right: More tips")

    subtitle = "  |  ".join(subtitle_parts) if subtitle_parts else None

    return Panel(
        content,
        title="[bold yellow]? Help[/bold yellow]",
        subtitle=f"[dim]{subtitle}[/dim]" if subtitle else None,
        border_style="yellow",
        padding=(0, 1),
    )
