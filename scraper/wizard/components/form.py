"""Reusable form/checkbox component."""

import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from scraper.wizard.components.menu import get_key

if TYPE_CHECKING:
    from scraper.wizard.components.help_content import HelpEntry


@dataclass
class CheckboxField:
    """A checkbox field in a form."""

    label: str
    description: str = ""
    value: bool = False
    help: "HelpEntry | None" = None
    checked_label: str = "[X]"
    unchecked_label: str = "[ ]"


@dataclass
class NumberField:
    """A number input field in a form."""

    label: str
    description: str = ""
    value: int | None = None
    min_value: int | None = None
    max_value: int | None = None
    allow_none: bool = True
    help: "HelpEntry | None" = None
    depends_on: str | None = None  # Label of a CheckboxField that must be True


@dataclass
class FormField:
    """Generic form field wrapper."""

    checkbox: CheckboxField | None = None
    number: NumberField | None = None


class Form:
    """Interactive form with checkboxes and number inputs."""

    def __init__(
        self,
        title: str,
        fields: list[CheckboxField | NumberField],
        submit_label: str = "Continue",
        extra_actions: list[tuple[str, str, str]] | None = None,
    ):
        """
        Initialize the form.

        Args:
            title: Form title
            fields: List of form fields
            submit_label: Label for the submit action
            extra_actions: List of (key, label, action_id) for extra actions
        """
        self.title = title
        self.fields = fields
        self.submit_label = submit_label
        self.extra_actions = extra_actions or []
        self.cursor = 0
        self.console = Console()
        self.editing_number = False
        self.number_buffer = ""
        self.help_mode = False
        self.help_page = 0

    def _is_field_visible(self, field: CheckboxField | NumberField) -> bool:
        """Check if a field should be visible based on its dependencies."""
        if isinstance(field, NumberField) and field.depends_on:
            # Find the dependency field and check its value
            for f in self.fields:
                if f.label == field.depends_on:
                    if isinstance(f, CheckboxField):
                        return f.value
                    return False
            return False
        return True

    def _get_visible_fields(self) -> list[tuple[int, CheckboxField | NumberField]]:
        """Get list of (original_index, field) for visible fields only."""
        return [
            (i, f) for i, f in enumerate(self.fields) if self._is_field_visible(f)
        ]

    def _build_display(self) -> Panel:
        """Build the form display."""
        from scraper.wizard.components.help_box import build_help_box

        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("", width=3)  # Cursor
        table.add_column("", width=5)  # Checkbox/value
        table.add_column("", width=35)  # Label
        table.add_column("")  # Description

        help_box = None
        visible_fields = self._get_visible_fields()

        for visible_idx, (orig_idx, fld) in enumerate(visible_fields):
            cursor = ">" if visible_idx == self.cursor else " "
            cursor_style = "bold cyan" if visible_idx == self.cursor else ""

            cursor_cell = f"[{cursor_style}]{cursor}[/{cursor_style}]" if cursor_style else cursor

            if isinstance(fld, CheckboxField):
                # Escape the labels to prevent Rich markup interpretation
                if fld.value:
                    checkbox = f"[bold green]{escape(fld.checked_label)}[/bold green]"
                else:
                    checkbox = escape(fld.unchecked_label)
                label = escape(fld.label)
                if visible_idx == self.cursor:
                    label = f"[reverse]{label}[/reverse]"
                desc = f"[dim]{escape(fld.description)}[/dim]" if fld.description else ""
                table.add_row(cursor_cell, checkbox, label, desc)

            elif isinstance(fld, NumberField):
                if self.editing_number and visible_idx == self.cursor:
                    value_str = f"[cyan]{self.number_buffer}_[/cyan]"
                elif fld.value is not None:
                    value_str = f"[cyan]{fld.value}[/cyan]"
                else:
                    value_str = "[dim]--[/dim]"
                label = escape(fld.label)
                if visible_idx == self.cursor:
                    label = f"[reverse]{label}[/reverse]"
                desc = f"[dim]{escape(fld.description)}[/dim]" if fld.description else ""
                table.add_row(cursor_cell, value_str, label, desc)

            # Insert help box after cursor item when in help mode
            if self.help_mode and visible_idx == self.cursor and fld.help is not None:
                help_box = build_help_box(fld.help, self.help_page)

        # Help text
        help_text = Text()
        help_text.append("\n")
        help_text.append("Up/Down", style="bold cyan")
        help_text.append(": Navigate  ", style="dim")
        help_text.append("Space", style="bold cyan")
        help_text.append(": Toggle  ", style="dim")
        help_text.append("Enter", style="bold cyan")
        help_text.append(f": {self.submit_label}  ", style="dim")

        for key, label, _ in self.extra_actions:
            help_text.append(key.upper(), style="bold cyan")
            help_text.append(f": {label}  ", style="dim")

        help_text.append("H", style="bold cyan")
        help_text.append(": Help  ", style="dim")
        help_text.append("Q", style="bold cyan")
        help_text.append(": Quit", style="dim")

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

    def show(self) -> dict[str, Any] | str | None:
        """
        Show the form and return field values.

        Returns:
            - Dict of field label -> value if submitted
            - Action ID string if extra action selected
            - None if cancelled
        """
        with Live(
            console=self.console, refresh_per_second=30, screen=False
        ) as live:
            while True:
                live.update(self._build_display())
                key = get_key()

                visible_fields = self._get_visible_fields()
                if not visible_fields:
                    # No visible fields, just allow submit/quit
                    if key == "enter":
                        return {f.label: self._get_value(f) for f in self.fields}
                    elif key == "q":
                        return None
                    continue

                # Clamp cursor to visible range
                self.cursor = min(self.cursor, len(visible_fields) - 1)
                _, current_field = visible_fields[self.cursor]

                if self.editing_number:
                    if key == "enter":
                        if isinstance(current_field, NumberField):
                            if self.number_buffer:
                                try:
                                    value = int(self.number_buffer)
                                    if current_field.min_value is not None:
                                        value = max(value, current_field.min_value)
                                    if current_field.max_value is not None:
                                        value = min(value, current_field.max_value)
                                    current_field.value = value
                                except ValueError:
                                    pass
                            elif current_field.allow_none:
                                current_field.value = None
                        self.editing_number = False
                        self.number_buffer = ""
                    elif key == "escape":
                        self.editing_number = False
                        self.number_buffer = ""
                    elif key == "backspace":
                        self.number_buffer = self.number_buffer[:-1]
                    elif key.isdigit():
                        self.number_buffer += key
                    continue

                if key == "up":
                    self.cursor = max(0, self.cursor - 1)
                    self.help_page = 0  # Reset page on navigation
                elif key == "down":
                    self.cursor = min(len(visible_fields) - 1, self.cursor + 1)
                    self.help_page = 0  # Reset page on navigation
                elif key == "left" and self.help_mode:
                    # Page backwards through tips
                    if current_field.help:
                        self.help_page = (self.help_page - 1) % current_field.help.page_count
                elif key == "right" and self.help_mode:
                    # Page forwards through tips
                    if current_field.help:
                        self.help_page = (self.help_page + 1) % current_field.help.page_count
                elif key == "h":
                    if self.help_mode:
                        self.help_mode = False
                    else:
                        self.help_mode = True
                        self.help_page = 0
                elif key == "space":
                    if self.help_mode:
                        self.help_mode = False
                    elif isinstance(current_field, CheckboxField):
                        current_field.value = not current_field.value
                        # Clamp cursor if toggling hid a field we were past
                        new_visible = self._get_visible_fields()
                        self.cursor = min(self.cursor, len(new_visible) - 1)
                    elif isinstance(current_field, NumberField):
                        self.editing_number = True
                        self.number_buffer = (
                            str(current_field.value)
                            if current_field.value is not None
                            else ""
                        )
                elif key == "enter":
                    if self.help_mode:
                        self.help_mode = False
                    else:
                        # Always submit form on Enter
                        return {f.label: self._get_value(f) for f in self.fields}
                elif key == "q":
                    if self.help_mode:
                        self.help_mode = False
                    else:
                        return None
                else:
                    # Check extra actions
                    for action_key, _, action_id in self.extra_actions:
                        if key.lower() == action_key.lower():
                            return action_id

    def _get_value(self, field: CheckboxField | NumberField) -> Any:
        """Get the value from a field."""
        if isinstance(field, CheckboxField):
            return field.value
        elif isinstance(field, NumberField):
            return field.value
        return None
