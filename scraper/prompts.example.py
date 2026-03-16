"""
LLM prompt templates for the price comparison pipeline.

Copy this file to prompts.py and customise the prompts for your use case.
Each variable is a string template with {placeholders} for dynamic data.
"""

# Item evaluation prompt: receives {item_json}, {item_id}
ITEM_EVALUATION_PROMPT = """Evaluate which search result best matches the target product.

[Your matching criteria and quality rating rules here]

Item data:
{item_json}

Respond with ONLY this JSON (no other text):
{{"id": {item_id}, "our_pack_qty": <number>, "store_a_match_rank": <1-3 or null>, "store_a_pack_qty": <number or null>, "store_a_per_qty": <number or null>, "store_a_per_weight_g": <number or null>, "store_a_qty_multiplier": <number or null>, "store_a_quality": "<quality>", "store_a_reason": "<brief>", "store_b_match_rank": <1-3 or null>, "store_b_pack_qty": <number or null>, "store_b_per_qty": <number or null>, "store_b_per_weight_g": <number or null>, "store_b_qty_multiplier": <number or null>, "store_b_quality": "<quality>", "store_b_reason": "<brief>"}}"""


# Search term generation prompt: receives {items_json}
SEARCH_TERM_PROMPT = """Generate search terms for price comparison.

[Your rules for transforming product names into search terms here]

Items:
{items_json}

Reply with JSON only, no other text:
{{"terms": [{{"id": 123, "search_term": "..."}}, ...]}}"""


# Search term + weight extraction prompt: receives {items_json}
SEARCH_TERM_WEIGHT_PROMPT = """Generate search terms and extract weights for price comparison.

[Your rules for search terms, weight extraction, and per_qty here]

Items:
{items_json}

Reply with JSON only:
{{"items": [{{"id": 123, "search_term": "...", "weight_g": 157, "per_qty": 1}}, ...]}}"""


# SWOT analysis prompt: receives {store_a_name}, {store_b_name}, {data_summary}
SWOT_LLM_PROMPT = (
    "You are a pricing analyst. "
    "Generate a SWOT analysis based on this price comparison data:\n\n"
    "{data_summary}\n"
    "[Your SWOT rules here]\n\n"
    "Return ONLY valid JSON in this exact format, no other text:\n"
    '{{"strengths": ["..."], "weaknesses": ["..."], "opportunities": ["..."], "threats": ["..."]}}'
)
