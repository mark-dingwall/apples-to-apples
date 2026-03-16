import logging
import re
from urllib.parse import quote_plus

from playwright.async_api import BrowserContext, TimeoutError as PlaywrightTimeout

from scraper import config
from scraper.models import BlockedError, SearchResult
from scraper.stores.base import BaseStoreScraper
from scraper.utils.matching import calculate_confidence

try:
    from scraper.stores.store_config import STORE_B as STORE_CONFIG
except ImportError:
    raise ImportError(
        "Store config not found. Copy scraper/stores/store_config.example.py "
        "to scraper/stores/store_config.py and fill in your store details."
    )

logger = logging.getLogger(__name__)


class StoreBScraper(BaseStoreScraper):
    """Scraper for Store B website."""

    name = STORE_CONFIG["display_name"]

    def __init__(self, context: BrowserContext, debug_mouse: bool = False):
        super().__init__(context, debug_mouse=debug_mouse)

    def get_search_url(self, query: str) -> str:
        return STORE_CONFIG["search_url"].format(query=quote_plus(query))

    async def wait_for_results(self) -> bool:
        """Wait for product tiles to load (including shadow DOM content)."""
        sel = STORE_CONFIG["selectors"]
        try:
            # Wait for either no-results message OR product tiles to appear
            await self.page.wait_for_selector(
                f"{sel['no_results']}, {sel['product_tile']}",
                timeout=config.SELECTOR_TIMEOUT
            )

            # Now check which one appeared - no-results takes priority
            no_results_el = await self.page.query_selector(sel["no_results"])
            if no_results_el:
                return False

            # Wait for shadow DOM content to render (price indicates fully loaded)
            # The >> operator pierces shadow DOM boundaries
            await self.page.wait_for_selector(
                f"{sel['product_tile']} >> {sel['product_price']}",
                timeout=config.SELECTOR_TIMEOUT
            )
            return True
        except PlaywrightTimeout:
            logger.warning(f"[{self.name}] Timeout waiting for product tiles")
            # Check if blocked
            blocked = await self.page.query_selector("text=Access Denied")
            if blocked:
                raise BlockedError(self.name, "Access Denied page detected")
            return False
        except Exception as e:
            logger.warning(f"[{self.name}] Error waiting for results: {e}")
            # Check if blocked
            blocked = await self.page.query_selector("text=Access Denied")
            if blocked:
                raise BlockedError(self.name, "Access Denied page detected")
            return False

    async def parse_results(self, search_term: str) -> tuple[list[SearchResult], int]:
        """Parse product tiles from Store B search results (with shadow DOM)."""
        results = []
        skipped_no_price = 0
        sel = STORE_CONFIG["selectors"]
        url_pattern = STORE_CONFIG["url_pattern"]

        # Use locator API which handles shadow DOM with >> operator
        tiles = self.page.locator(sel["product_tile"])
        count = await tiles.count()

        # Check more tiles than needed - some may lack prices (OOS items)
        for i in range(min(count, config.MAX_TILES_TO_CHECK)):
            if len(results) >= config.MAX_RESULTS_PER_STORE:
                break

            try:
                tile = tiles.nth(i)

                # Extract price first - skip tiles without prices (can't compare)
                price_locator = tile.locator(sel["product_price"])
                price_count = await price_locator.count()
                price = ""
                if price_count > 0:
                    price_text = await price_locator.first.text_content()
                    price = self._clean_price(price_text) if price_text else ""

                if not price or "$" not in price:
                    skipped_no_price += 1
                    logger.debug(f"[{self.name}] Skipping tile without price")
                    continue

                rank = len(results) + 1

                # Product name - Playwright locator API auto-pierces shadow DOM
                name_locator = tile.locator(sel["product_name"])
                name = await name_locator.first.text_content() if await name_locator.count() > 0 else ""

                # Unit price
                unit_price_locator = tile.locator(sel["product_unit_price"])
                unit_price = await unit_price_locator.first.text_content() if await unit_price_locator.count() > 0 else ""

                # Product URL
                link_locator = tile.locator(sel["product_link"])
                url = ""
                product_id = ""
                if await link_locator.count() > 0:
                    href = await link_locator.first.get_attribute("href")
                    if href:
                        base = url_pattern["product_url_prefix"]
                        url = f"{base}{href}" if href.startswith("/") else href
                        # Extract product ID from URL
                        match = re.search(url_pattern["product_id_regex"], href)
                        product_id = match.group(1) if match else ""

                # Product image URL
                img_locator = tile.locator(sel["product_image"])
                image_url = ""
                if await img_locator.count() > 0:
                    image_url = await img_locator.first.get_attribute("src") or ""

                # Weight info from aria-label on product image link
                weight_info = ""
                if "product_image_link" in sel:
                    img_link_locator = tile.locator(sel["product_image_link"])
                    if await img_link_locator.count() > 0:
                        weight_info = await img_link_locator.first.get_attribute("aria-label") or ""

                # Check for special/sale - look for was-price element or text indicators
                special_locator = tile.locator(sel["special_badge"])
                is_on_special = await special_locator.count() > 0

                # Also check for "was" price indicator in visible text
                if not is_on_special:
                    try:
                        full_text = await tile.text_content() or ""
                        is_on_special = "was $" in full_text.lower() or "save $" in full_text.lower()
                    except PlaywrightTimeout:
                        logger.warning(f"[{self.name}] Timeout checking special price for tile {rank}")
                    except Exception as e:
                        logger.warning(f"[{self.name}] Error checking special price: {e}")

                # Check availability - item is unavailable if no price or explicit unavailable tag
                unavailable_locator = tile.locator(sel["unavailable"])
                has_unavailable_tag = await unavailable_locator.count() > 0
                is_available = bool(price) and not has_unavailable_tag

                # Get HTML snippet (outer HTML of the web component)
                html_snippet = await tile.evaluate("el => el.outerHTML")

                # Calculate confidence score
                confidence = calculate_confidence(search_term, name or "")

                results.append(SearchResult(
                    rank=rank,
                    name=(name or "").strip(),
                    price=(price or "").strip(),
                    unit_price=(unit_price or "").strip(),
                    url=url,
                    is_on_special=is_on_special,
                    is_available=is_available,
                    confidence_score=confidence,
                    html_snippet=html_snippet,
                    product_id=product_id,
                    image_url=image_url,
                    weight_info=weight_info,
                ))

            except PlaywrightTimeout as e:
                logger.warning(f"[{self.name}] Timeout parsing tile {len(results) + 1}: {e}")
                continue
            except Exception as e:
                logger.warning(f"[{self.name}] Error parsing tile {len(results) + 1}: {e}")
                continue

        if skipped_no_price > 0:
            logger.info(f"[{self.name}] Skipped {skipped_no_price} tiles without prices")

        return results, skipped_no_price

    def _clean_price(self, price: str) -> str:
        """Clean up price text."""
        price = price.strip()
        lines = price.split("\n")
        for line in lines:
            line = line.strip()
            if "$" in line and any(c.isdigit() for c in line):
                return line
        return price
