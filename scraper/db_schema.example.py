"""
Database schema configuration template.
Copy to db_schema.py and customise for your database.

Your database needs a table with at minimum:
- Item ID, name, price, category, and an RRP field to update
- An offer/batch grouping field
- Soft-delete support (deleted_at column)
"""

FETCH_ITEMS = """
    SELECT id, name, price, category_id
    FROM products
    WHERE batch_id = %s
      AND category_id IN (1, 2)
      AND deleted_at IS NULL
    ORDER BY id
"""

UPDATE_RRP = "UPDATE products SET rrp = %s, updated_at = NOW() WHERE id = %s"

VERIFY_OFFER = """
    SELECT COUNT(*) as cnt
    FROM products
    WHERE batch_id = %s
      AND category_id IN (1, 2)
      AND deleted_at IS NULL
"""

FETCH_RECENT_OFFERS = """
    SELECT batch_id, COUNT(*) as cnt, MAX(updated_at) as latest
    FROM products
    WHERE category_id IN (1, 2)
      AND deleted_at IS NULL
    GROUP BY batch_id
    ORDER BY batch_id DESC
    LIMIT %s
"""

FETCH_CURRENT_RRP = """
    SELECT id, rrp
    FROM products
    WHERE id IN ({placeholders})
"""

# Column name mapping (must match your query aliases)
COL_ID = "id"
COL_NAME = "name"
COL_PRICE = "price"
COL_CATEGORY = "category_id"
COL_OFFER = "batch_id"
COL_RRP = "rrp"
COL_COUNT = "cnt"
COL_LATEST = "latest"

# Category IDs for your product types
FRUIT_CATEGORY = 1
VEG_CATEGORY = 2

# Category ID to display name mapping (used in LLM prompts)
CATEGORY_NAMES = {
    1: "Fruit",
    2: "Vegetables",
}
