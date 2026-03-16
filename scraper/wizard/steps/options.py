"""Step 3: Run options and settings editor."""

import logging
import subprocess
import sys
from pathlib import Path

from rich.console import Console

from scraper.wizard.components.form import CheckboxField, Form, NumberField
from scraper.wizard.components.help_content import HelpEntry, HelpTip
from scraper.wizard.settings import Settings, SettingsManager
from scraper.wizard.state import WizardState

logger = logging.getLogger(__name__)


# Help content for form fields
HEADLESS_HELP = HelpEntry([
    HelpTip(
        "Run the browser without a visible window. Slightly faster but you won't "
        "see the scraping in action. If scraping fails, try disabling this to "
        "debug what's happening on the page."
    ),
])

LIMIT_HELP = HelpEntry([
    HelpTip(
        "Process only a subset of items instead of the full list. Useful for "
        "testing your setup or when you only need a quick spot-check."
    ),
])

LIMIT_COUNT_HELP = HelpEntry([
    HelpTip(
        "How many items to process. Press Space to edit, then Enter to confirm."
    ),
])


def open_settings_editor(settings_path: Path) -> None:
    """Open settings.json in default editor."""
    console = Console()

    try:
        if sys.platform == "win32":
            # Use start command on Windows
            subprocess.run(["start", "", str(settings_path)], shell=True)
        elif sys.platform == "darwin":
            # Use open command on macOS
            subprocess.run(["open", str(settings_path)])
        else:
            # Try common editors on Linux
            editor = subprocess.run(
                ["which", "code"], capture_output=True, text=True
            ).stdout.strip()
            if not editor:
                editor = subprocess.run(
                    ["which", "nano"], capture_output=True, text=True
                ).stdout.strip()
            if not editor:
                editor = subprocess.run(
                    ["which", "vi"], capture_output=True, text=True
                ).stdout.strip()

            if editor:
                subprocess.Popen([editor, str(settings_path)])
            else:
                console.print(f"[yellow]Please edit manually: {settings_path}[/yellow]")
    except Exception as e:
        console.print(f"[yellow]Could not open editor: {e}[/yellow]")
        console.print(f"Please edit manually: {settings_path}")


def run_options(state: WizardState, settings_manager: SettingsManager) -> bool:
    """
    Show options form and settings editor.

    Returns:
        True to continue, False if cancelled.
    """
    settings = settings_manager.load()

    needs_browser = not state.use_cached_results

    while True:
        # Build form fields from settings
        fields = []

        if needs_browser:
            fields.append(
                CheckboxField(
                    label="Headless browser",
                    description="Run browser without visible window",
                    value=settings.default_headless,
                    help=HEADLESS_HELP,
                ),
            )

        fields.append(
            CheckboxField(
                label="Limit items?",
                description="Process only a subset for testing",
                value=False,
                help=LIMIT_HELP,
            ),
        )

        fields.append(
            NumberField(
                label="How many?",
                description="Number of items to process",
                value=5,
                min_value=1,
                max_value=1000,
                allow_none=False,
                help=LIMIT_COUNT_HELP,
                depends_on="Limit items?",
            ),
        )

        form = Form(
            title=f"Run Options - Offer {state.offer_id} ({state.item_count} items)",
            fields=fields,
            submit_label="Start",
            extra_actions=[("e", "Edit Settings", "edit_settings")],
        )

        result = form.show()

        if result is None:
            return False

        if result == "edit_settings":
            open_settings_editor(settings_manager.path)
            # Reload settings after editing
            settings_manager._settings = None
            settings = settings_manager.load()
            continue

        # Extract values
        state.headless = result.get("Headless browser", False)
        if result.get("Limit items?", False):
            state.limit = result.get("How many?", 5)
        else:
            state.limit = None
        state.batch_size = settings.default_batch_size

        logger.info(
            f"Options: headless={state.headless}, "
            f"limit={state.limit}, batch_size={state.batch_size}"
        )

        return True
