"""Wizard runner that orchestrates all steps."""

import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.markup import escape

from scraper.wizard.settings import SettingsManager
from scraper.wizard.state import WizardState
from scraper.wizard.steps.approval import run_approval
from scraper.wizard.steps.cache_check import run_cache_check
from scraper.wizard.steps.offer_select import run_offer_select
from scraper.wizard.steps.options import run_options
from scraper.wizard.steps.progress import run_progress
from scraper.wizard.steps.report import run_report

logger = logging.getLogger(__name__)


class WizardRunner:
    """Orchestrates the wizard steps."""

    def __init__(self, settings_path: Path | None = None):
        self.settings_manager = SettingsManager(settings_path)
        self.state = WizardState()
        self.console = Console()

    def run(self) -> int:
        """
        Run the wizard.

        Returns:
            Exit code (0 for success, non-zero for error/cancellation).
        """
        # Load settings
        settings = self.settings_manager.load()
        output_dir = Path(settings.output_dir)

        # Initialize state with defaults from settings
        self.state.headless = settings.default_headless
        self.state.batch_size = settings.default_batch_size
        self.state.search_term_batch_size = settings.search_term_batch_size

        self.console.print()
        self.console.print("[bold]Fruit & Veg Price Scraper[/bold]")
        self.console.print("[dim]Interactive wizard for price comparison[/dim]")
        self.console.print()

        try:
            # Step 1: Offer selection
            if not run_offer_select(self.state):
                self.console.print("[dim]Cancelled.[/dim]")
                return 1

            # Step 2: Cache check
            if not run_cache_check(self.state, output_dir):
                self.console.print("[dim]Cancelled.[/dim]")
                return 1

            # Step 3: Options
            if not run_options(self.state, self.settings_manager):
                self.console.print("[dim]Cancelled.[/dim]")
                return 1

            # Step 4: Progress (run pipeline)
            if not run_progress(self.state, output_dir):
                self.console.print("[red]Pipeline failed.[/red]")
                return 1

            # Step 5: Approval
            if not run_approval(self.state):
                self.console.print("[dim]Cancelled.[/dim]")
                return 1

            # Step 6: Report
            if not run_report(self.state, settings, output_dir):
                self.console.print("[red]Report failed.[/red]")
                return 1

            return 0

        except KeyboardInterrupt:
            self.console.print("\n[dim]Interrupted.[/dim]")
            return 130

        except Exception as e:
            logger.exception("Wizard failed with unexpected error")
            self.console.print(f"\n[red]Error: {escape(str(e))}[/red]")
            return 1


def main() -> int:
    """Entry point for the wizard."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    runner = WizardRunner()
    return runner.run()


if __name__ == "__main__":
    sys.exit(main())
