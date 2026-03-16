"""Step 1: Offer selection from database."""

import logging

from rich.console import Console

from scraper.db import OfferInfo, fetch_recent_offers, verify_offer_exists
from scraper.wizard.components.help_content import HelpEntry, HelpTip
from scraper.wizard.components.menu import Menu, MenuItem
from scraper.wizard.state import WizardState

logger = logging.getLogger(__name__)


# Help content for offer selection
OFFER_HELP = HelpEntry([
    HelpTip(
        "Select an offer to process. Each offer represents a batch of fruit & "
        "vegetable items from your database. The item count shows how many "
        "products will be scraped and compared against competitor store prices."
    ),
    HelpTip(
        "The 'Updated' timestamp shows when prices were last modified in the "
        "database. Choose a recent offer to ensure you're working with current "
        "inventory data."
    ),
])

RECOMMENDED_HELP = HelpEntry([
    HelpTip(
        "This offer has the highest ID, meaning it's the most recently created. "
        "Unless you have a specific reason to process an older offer, this is "
        "usually the best choice."
    ),
])


def validate_offer_id(value: str) -> int | None:
    """Validate manual offer ID entry."""
    try:
        offer_id = int(value.strip())
        if offer_id <= 0:
            return None
        if verify_offer_exists(offer_id):
            return offer_id
        return None
    except ValueError:
        return None


def run_offer_select(state: WizardState) -> bool:
    """
    Run the offer selection step.

    Returns:
        True if an offer was selected, False if cancelled.
    """
    console = Console()

    console.print("\n[bold]Connecting to database...[/bold]")

    try:
        offers = fetch_recent_offers(limit=3)
    except Exception as e:
        console.print(f"[red]Failed to connect to database: {e}[/red]")
        console.print("\nPlease check your .env configuration and try again.")
        return False

    if not offers:
        console.print("[yellow]No offers found with F&V items.[/yellow]")
        return False

    # Build menu items
    items: list[MenuItem[int]] = []
    for i, offer in enumerate(offers):
        updated_str = ""
        if offer.latest_updated:
            updated_str = offer.latest_updated.strftime("%Y-%m-%d %H:%M")

        # First item (highest offer_id) is recommended
        badge = "Recommended" if i == 0 else ""
        help_entry = RECOMMENDED_HELP if i == 0 else OFFER_HELP

        items.append(
            MenuItem(
                label=f"Offer {offer.offer_id}",
                description=f"{offer.item_count} items | Updated: {updated_str}",
                value=offer.offer_id,
                badge=badge,
                help=help_entry,
            )
        )

    menu = Menu(
        title="Select Offer to Process",
        items=items,
        allow_manual_entry=True,
        manual_entry_label="Enter offer ID manually...",
        manual_entry_prompt="Offer ID: ",
        manual_entry_validator=validate_offer_id,
    )

    result = menu.show()

    if result is None:
        return False

    state.offer_id = result

    # Get item count for selected offer
    for offer in offers:
        if offer.offer_id == result:
            state.item_count = offer.item_count
            break
    else:
        # Manual entry - fetch count
        if verify_offer_exists(result):
            # Re-fetch to get count
            for offer in fetch_recent_offers(limit=100):
                if offer.offer_id == result:
                    state.item_count = offer.item_count
                    break

    logger.info(f"Selected offer {state.offer_id} with {state.item_count} items")
    return True
