"""
Entry point: python -m scraper

Launches TUI wizard by default.
Falls back to CLI mode if flags provided for backwards compatibility.
"""

import sys


def main() -> int:
    """Main entry point."""
    # If CLI flags provided, delegate to existing pipeline
    if len(sys.argv) > 1:
        # Check if any arguments look like flags
        has_flags = any(arg.startswith("-") for arg in sys.argv[1:])

        if has_flags:
            from scraper.pipeline import main as pipeline_main

            pipeline_main()
            return 0

    # Otherwise launch wizard
    from scraper.wizard.runner import main as wizard_main

    return wizard_main()


if __name__ == "__main__":
    sys.exit(main())
