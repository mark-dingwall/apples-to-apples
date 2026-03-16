"""
Store configuration template.
Copy this file to store_config.py and fill in your values.

Each store needs:
- display_name: Human-readable name
- search_url: URL template with {query} placeholder
- base_url: Site base URL for constructing full URLs
- locale/timezone: Browser locale settings
- selectors: CSS selectors for scraping product data
- uses_shadow_dom: Whether products are inside shadow DOM (requires >> piercing)
- url_pattern: Regex for extracting product IDs from URLs
- add_button_selector: CSS selector for the add-to-cart button (used in OOS detection)
"""

STORE_A = {
    "display_name": "Store A",
    "search_url": "https://store-a.example.com/search?q={query}",
    "base_url": "https://store-a.example.com",
    "locale": "en-US",
    "timezone": "America/New_York",
    "selectors": {
        "product_tile": ".product-card",
        "product_name": ".product-title",
        "product_price": ".product-price",
        "product_link": "a.product-link",
        "product_unit_price": ".unit-price",
        "special_badge": ".sale-badge",
        "unavailable": ".out-of-stock",
        "product_image": "img.product-image",
        "no_results": ".no-results-message",
    },
    "uses_shadow_dom": False,
    "url_pattern": {
        "product_id_regex": r"/product/(\d+)",
        "product_url_prefix": "https://store-a.example.com",
    },
    "add_button_selector": "button.add-to-cart",
}

STORE_B = {
    "display_name": "Store B",
    "search_url": "https://store-b.example.com/search?term={query}",
    "base_url": "https://store-b.example.com",
    "locale": "en-US",
    "timezone": "America/New_York",
    "selectors": {
        "product_tile": ".product-tile",
        "product_name": ".product-name a",
        "product_price": ".price .primary",
        "product_link": ".product-name a",
        "product_unit_price": ".price-per-unit",
        "special_badge": ".was-price",
        "unavailable": ".unavailable-tag",
        "product_image": ".product-image img",
        "product_image_link": ".product-image a",
        "no_results": ".zero-results",
    },
    "uses_shadow_dom": True,
    "url_pattern": {
        "product_id_regex": r"/product/(\d+)/",
        "product_url_prefix": "https://store-b.example.com",
    },
    "add_button_selector": "button.add-to-cart, button:has-text('Add to cart')",
}

STORE_A_NAME = STORE_A["display_name"]
STORE_B_NAME = STORE_B["display_name"]
STORE_A_COL = STORE_A_NAME.lower().replace(" ", "_")
STORE_B_COL = STORE_B_NAME.lower().replace(" ", "_")
