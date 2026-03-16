import re
from rapidfuzz import fuzz


# Common unit suffixes to strip from item names
UNIT_PATTERNS = [
    r"\s+per\s+kg\b",
    r"\s+per\s+100g\b",
    r"\s+per\s+each\b",
    r"\s+ea\b",
    r"\s+each\b",
    r"\s+bunch\b",
    r"\s+punnet\b",
    r"\s+half\b",
    r"\s+kg\b",
    r"\s*\(approx\.?\s*\d+g?\)",
    r"\s*\(approx\.?\s*\d+-\d+g?\)",
    r"\s*\(\d+g?\)",
    r"\s*-\s*\d+g\b",
]

# Pre-compile patterns for performance
COMPILED_UNIT_PATTERNS = [re.compile(p, re.IGNORECASE) for p in UNIT_PATTERNS]
COMPILED_WHITESPACE = re.compile(r"\s+")


def extract_search_term(name: str) -> str:
    """
    Extract a clean search term from an item name by removing unit suffixes.

    Example: "Apples Royal Gala per kg" -> "Apples Royal Gala"
    """
    result = name.strip()

    for pattern in COMPILED_UNIT_PATTERNS:
        result = pattern.sub("", result)

    result = COMPILED_WHITESPACE.sub(" ", result).strip()

    return result


def calculate_confidence(search_term: str, result_name: str) -> float:
    """
    Calculate confidence score between search term and result name.
    Returns a score between 0.0 and 1.0.
    """
    search_clean = search_term.lower().strip()
    result_clean = result_name.lower().strip()

    if not search_clean or not result_clean:
        return 0.0

    # Use token set ratio which handles word order differences well
    # e.g., "Royal Gala Apples" vs "Apples Royal Gala"
    token_score = fuzz.token_set_ratio(search_clean, result_clean)

    # Also check partial ratio for substring matches
    partial_score = fuzz.partial_ratio(search_clean, result_clean)

    # Combine scores: weight token_set 70% (handles word order) and partial 30%
    # (catches substrings). Weights chosen empirically for produce names.
    combined = (token_score * 0.7 + partial_score * 0.3) / 100.0

    return round(combined, 2)
