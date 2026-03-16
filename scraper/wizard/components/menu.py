"""Reusable selectable menu component."""

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Generic, TypeVar

from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from scraper.wizard.components.help_content import HelpEntry

T = TypeVar("T")


@dataclass
class MenuItem(Generic[T]):
    """A menu item with label, description, and associated value."""

    label: str
    description: str = ""
    value: T = None  # type: ignore
    badge: str = ""  # Optional badge (e.g., "Recommended")
    help: "HelpEntry | None" = None  # Optional contextual help


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
        elif key == b"\x03":  # Ctrl+C
            raise KeyboardInterrupt
        elif key == b"\x1b":  # Escape
            return "escape"
        elif key == b"\x08":  # Backspace
            return "backspace"
        try:
            return key.decode("utf-8", errors="ignore")
        except (UnicodeDecodeError, AttributeError):
            return ""
    else:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":  # Escape sequence
                ch2 = sys.stdin.read(1)
                if ch2 == "[":
                    ch3 = sys.stdin.read(1)
                    if ch3 == "A":
                        return "up"
                    elif ch3 == "B":
                        return "down"
                    elif ch3 == "C":
                        return "right"
                    elif ch3 == "D":
                        return "left"
                return "escape"
            elif ch == "\r" or ch == "\n":
                return "enter"
            elif ch == " ":
                return "space"
            elif ch.lower() == "q":
                return "q"
            elif ch == "\x7f":  # Backspace
                return "backspace"
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


class Menu(Generic[T]):
    """Interactive menu with keyboard navigation."""

    def __init__(
        self,
        title: str,
        items: list[MenuItem[T]],
        allow_manual_entry: bool = False,
        manual_entry_label: str = "Enter manually...",
        manual_entry_prompt: str = "Enter value: ",
        manual_entry_validator: Callable[[str], T | None] | None = None,
    ):
        self.title = title
        self.items = items
        self.allow_manual_entry = allow_manual_entry
        self.manual_entry_label = manual_entry_label
        self.manual_entry_prompt = manual_entry_prompt
        self.manual_entry_validator = manual_entry_validator
        self.cursor = 0
        self.console = Console()
        self.manual_entry_mode = False
        self.manual_entry_buffer = ""
        self.validation_error: str | None = None
        self.help_mode = False
        self.help_page = 0

    def _build_display(self) -> Panel:
        """Build the menu display."""
        from scraper.wizard.components.help_box import build_help_box

        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("", width=3)
        table.add_column("", width=50)
        table.add_column("")

        help_box = None

        for i, item in enumerate(self.items):
            cursor = ">" if i == self.cursor else " "
            cursor_style = "bold cyan" if i == self.cursor else ""

            label = escape(item.label)
            if item.badge:
                label = f"{label} \\[{escape(item.badge)}]"

            if i == self.cursor:
                label = f"[reverse]{label}[/reverse]"

            desc = f"[dim]{escape(item.description)}[/dim]" if item.description else ""

            cursor_cell = f"[{cursor_style}]{cursor}[/{cursor_style}]" if cursor_style else cursor
            table.add_row(cursor_cell, label, desc)

            # Insert help box after cursor item when in help mode
            if self.help_mode and i == self.cursor and item.help is not None:
                help_box = build_help_box(item.help, self.help_page)

        # Manual entry option
        if self.allow_manual_entry:
            i = len(self.items)
            cursor = ">" if i == self.cursor else " "
            cursor_style = "bold cyan" if i == self.cursor else ""
            label = escape(self.manual_entry_label)
            if i == self.cursor:
                label = f"[reverse]{label}[/reverse]"
            cursor_cell = f"[{cursor_style}]{cursor}[/{cursor_style}]" if cursor_style else cursor
            table.add_row(cursor_cell, label, "")

        # Help text
        help_text = Text()
        help_text.append("\n")
        help_text.append("Up/Down", style="bold cyan")
        help_text.append(": Navigate  ", style="dim")
        help_text.append("Enter", style="bold cyan")
        help_text.append(": Select  ", style="dim")
        help_text.append("H", style="bold cyan")
        help_text.append(": Help  ", style="dim")
        help_text.append("Q", style="bold cyan")
        help_text.append(": Quit", style="dim")

        content = Text()
        content.append_text(help_text)

        # Combine table and help box if in help mode
        if help_box is not None:
            panel_content = Group(table, Text(""), help_box)
        else:
            panel_content = table

        return Panel(
            panel_content,
            title=f"[bold]{self.title}[/bold]",
            subtitle=str(help_text),
            border_style="blue",
        )

    def _build_manual_entry_display(self) -> Panel:
        """Build the manual entry display."""
        content = Text()
        content.append(f"\n{self.manual_entry_prompt}", style="bold")
        content.append(self.manual_entry_buffer, style="cyan")
        content.append("_", style="blink")

        # Show validation error if present
        if self.validation_error:
            content.append(f"\n[red]{self.validation_error}[/red]")

        content.append("\n\n")
        content.append("Enter", style="bold cyan")
        content.append(": Submit  ", style="dim")
        content.append("Escape", style="bold cyan")
        content.append(": Cancel", style="dim")

        return Panel(
            content,
            title=f"[bold]{self.title}[/bold]",
            border_style="red" if self.validation_error else "blue",
        )

    def show(self) -> T | None:
        """Show the menu and return the selected value, or None if cancelled."""
        total_items = len(self.items) + (1 if self.allow_manual_entry else 0)

        # Guard against empty menu with no manual entry option
        if total_items == 0:
            return None

        with Live(
            console=self.console, refresh_per_second=30, screen=False
        ) as live:
            while True:
                if self.manual_entry_mode:
                    live.update(self._build_manual_entry_display())
                else:
                    live.update(self._build_display())

                key = get_key()

                if self.manual_entry_mode:
                    if key == "enter":
                        if self.manual_entry_buffer:
                            if self.manual_entry_validator:
                                result = self.manual_entry_validator(
                                    self.manual_entry_buffer
                                )
                                if result is not None:
                                    self.validation_error = None
                                    return result
                                # Invalid input - show error
                                self.validation_error = "Invalid input. Please try again."
                            else:
                                return self.manual_entry_buffer  # type: ignore
                    elif key == "escape":
                        self.manual_entry_mode = False
                        self.manual_entry_buffer = ""
                        self.validation_error = None
                    elif key == "backspace":
                        self.manual_entry_buffer = self.manual_entry_buffer[:-1]
                        self.validation_error = None  # Clear error on edit
                    elif len(key) == 1 and key.isprintable():
                        self.manual_entry_buffer += key
                        self.validation_error = None  # Clear error on edit
                else:
                    if key == "up":
                        self.cursor = max(0, self.cursor - 1)
                        self.help_page = 0  # Reset page on navigation
                    elif key == "down":
                        self.cursor = min(total_items - 1, self.cursor + 1)
                        self.help_page = 0  # Reset page on navigation
                    elif key == "left" and self.help_mode:
                        # Page backwards through tips
                        current_item = self.items[self.cursor] if self.cursor < len(self.items) else None
                        if current_item and current_item.help:
                            self.help_page = (self.help_page - 1) % current_item.help.page_count
                    elif key == "right" and self.help_mode:
                        # Page forwards through tips
                        current_item = self.items[self.cursor] if self.cursor < len(self.items) else None
                        if current_item and current_item.help:
                            self.help_page = (self.help_page + 1) % current_item.help.page_count
                    elif key == "h":
                        if self.help_mode:
                            self.help_mode = False
                        else:
                            self.help_mode = True
                            self.help_page = 0
                    elif key == "enter":
                        if self.help_mode:
                            self.help_mode = False
                        elif self.allow_manual_entry and self.cursor == len(self.items):
                            self.manual_entry_mode = True
                        else:
                            return self.items[self.cursor].value
                    elif key == "q":
                        if self.help_mode:
                            self.help_mode = False
                        else:
                            return None
