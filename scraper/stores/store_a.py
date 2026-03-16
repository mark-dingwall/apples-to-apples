import logging
import re
from urllib.parse import quote_plus, unquote

from playwright.async_api import BrowserContext, TimeoutError as PlaywrightTimeout

from scraper import config
from scraper.models import BlockedError, SearchResult
from scraper.stores.base import BaseStoreScraper
from scraper.utils.matching import calculate_confidence

try:
    from scraper.stores.store_config import STORE_A as STORE_CONFIG
except ImportError:
    raise ImportError(
        "Store config not found. Copy scraper/stores/store_config.example.py "
        "to scraper/stores/store_config.py and fill in your store details."
    )

logger = logging.getLogger(__name__)


class StoreAScraper(BaseStoreScraper):
    """Scraper for Store A website."""

    name = STORE_CONFIG["display_name"]

    def __init__(self, context: BrowserContext, debug_mouse: bool = False):
        super().__init__(context, debug_mouse=debug_mouse)

    def get_search_url(self, query: str) -> str:
        return STORE_CONFIG["search_url"].format(query=quote_plus(query))

    async def wait_for_results(self) -> bool:
        """Wait for product tiles to load."""
        sel = STORE_CONFIG["selectors"]
        try:
            # First check for explicit "no results" page
            no_results_el = await self.page.query_selector(sel["no_results"])
            if no_results_el:
                text = await no_results_el.inner_text()
                if "No results for" in text:
                    return False

            await self.page.wait_for_selector(
                sel["product_tile"],
                timeout=config.SELECTOR_TIMEOUT
            )
            return True
        except PlaywrightTimeout:
            logger.warning(f"[{self.name}] Timeout waiting for product tiles")
            # Check if we got blocked
            blocked = await self.page.query_selector("text=Access Denied")
            if blocked:
                raise BlockedError(self.name, "Access Denied page detected")
            return False
        except Exception as e:
            logger.warning(f"[{self.name}] Error waiting for results: {e}")
            # Check if we got blocked
            blocked = await self.page.query_selector("text=Access Denied")
            if blocked:
                raise BlockedError(self.name, "Access Denied page detected")
            return False

    async def parse_results(self, search_term: str) -> tuple[list[SearchResult], int]:
        """Parse product tiles from Store A search results."""
        results = []
        skipped_no_price = 0
        sel = STORE_CONFIG["selectors"]
        url_pattern = STORE_CONFIG["url_pattern"]

        tiles = await self.page.query_selector_all(sel["product_tile"])

        # Check more tiles than needed - some may lack prices
        for tile in tiles[:config.MAX_TILES_TO_CHECK]:
            if len(results) >= config.MAX_RESULTS_PER_STORE:
                break

            try:
                # Extract price first - skip tiles without prices
                price_el = await tile.query_selector(sel["product_price"])
                price = await price_el.inner_text() if price_el else ""
                price = price.strip()

                if not price or "$" not in price:
                    skipped_no_price += 1
                    logger.debug(f"[{self.name}] Skipping tile without price")
                    continue

                rank = len(results) + 1

                # Product name
                name_el = await tile.query_selector(sel["product_name"])
                name = await name_el.inner_text() if name_el else ""

                # Unit price from dedicated element (price already extracted above)
                unit_price_el = await tile.query_selector(sel["product_unit_price"])
                unit_price = await unit_price_el.inner_text() if unit_price_el else ""
                unit_price = unit_price.strip()

                # Fallback: extract unit price from price element text if not found in dedicated element
                if not unit_price and price_el:
                    price_text = await price_el.inner_text()
                    price, unit_price = self._parse_price_text(price_text)

                # Product URL
                link_el = await tile.query_selector(sel["product_link"])
                url = ""
                product_id = ""
                weight_info = ""
                if link_el:
                    href = await link_el.get_attribute("href")
                    if href:
                        base = url_pattern["product_url_prefix"]
                        url = f"{base}{href}" if href.startswith("/") else href
                        # Extract product ID from URL
                        match = re.search(url_pattern["product_id_regex"], href)
                        product_id = match.group(1) if match else ""
                    # Weight info from aria-label
                    weight_info = await link_el.get_attribute("aria-label") or ""

                # Product image URL
                img_el = await tile.query_selector(sel["product_image"])
                image_url = ""
                if img_el:
                    image_url = await img_el.get_attribute("src") or ""
                    # Clean Next.js wrapper if present - extract original CDN URL
                    if image_url and "_next/image" in image_url:
                        match = re.search(r'url=([^&]+)', image_url)
                        if match:
                            image_url = unquote(match.group(1))

                # Check for special/sale badge
                special_el = await tile.query_selector(sel["special_badge"])
                is_on_special = special_el is not None

                # Also check if price text indicates special
                if not is_on_special and price:
                    is_on_special = "was" in price.lower() or "save" in price.lower()

                # Check availability - look for "currently unavailable" indicator
                unavailable_el = await tile.query_selector(sel["unavailable"])
                is_available = unavailable_el is None

                # Get HTML snippet
                html_snippet = await tile.inner_html()

                # Calculate confidence score
                confidence = calculate_confidence(search_term, name)

                results.append(SearchResult(
                    rank=rank,
                    name=name.strip(),
                    price=price,
                    unit_price=unit_price,
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

    def _parse_price_text(self, price_text: str) -> tuple[str, str]:
        """
        Parse price text to extract main price and unit price.
        Store A often shows prices like "$5.50 $1.10 per 100g"
        """
        lines = price_text.strip().split("\n")
        price = ""
        unit_price = ""

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # First price-like value is usually the main price
            if not price and "$" in line:
                price = line
            # Lines containing "per" are unit prices
            elif "per" in line.lower():
                unit_price = line
            # If we have price but this line also has $, might be unit price
            elif price and "$" in line and not unit_price:
                unit_price = line

        return price, unit_price
