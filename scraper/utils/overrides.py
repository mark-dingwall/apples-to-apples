"""
User-configurable search-term overrides.

Maps an OfferPart ``code`` prefix to a known-good search term (and optional manual weight),
so specific products always search competitor sites with a term that actually returns
results -- bypassing the LLM-generated term that sometimes yields nothing.

The override file (``search_overrides.json``, project root, gitignored) is OPTIONAL: if it is
absent or empty, behaviour is unchanged.

Format (code-prefix -> entry); an entry is either a bare string (search term only) or an
object with an optional numeric weight::

    {
        "LetBC030": "Cos Hearts",
        "TomTR250": {"search_term": "truss tomatoes", "our_weight_g": 250}
    }

Matching is by prefix (SQL ``code LIKE 'key%'``), so an optional week-specific code suffix
(e.g. "LetBC030δ") still matches the base key "LetBC030".
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from scraper.models import OfferPart

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path("search_overrides.json")


class MalformedOverridesError(Exception):
    """
    Raised when ``search_overrides.json`` is present but cannot be parsed or used
    (invalid JSON, wrong encoding, or not a JSON object).

    An *absent* file is not an error -- the feature is optional -- so callers can
    distinguish "the user isn't using overrides" from "the user is using them and made a
    mistake", and prompt or abort accordingly rather than silently running with overrides off.
    """


@dataclass
class ResolvedOverride:
    """A search-term override to apply to a matched item."""

    search_term: str
    weight_g: float | None = None


def load_search_overrides(path: Path | None = None) -> dict[str, ResolvedOverride]:
    """
    Load search-term overrides from JSON.

    Returns a mapping of code-prefix -> ResolvedOverride. Returns an empty dict if the file is
    **absent** (the feature is optional). Raises ``MalformedOverridesError`` if the file is
    **present but unusable** (invalid JSON, wrong encoding, or not a JSON object), so the
    caller can prompt/abort rather than silently running with overrides disabled. Malformed
    *individual* entries are skipped with a warning rather than failing the whole file.
    """
    path = path or DEFAULT_PATH

    if not path.exists():
        return {}

    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        raise MalformedOverridesError(f"Could not read search overrides from {path}: {e}") from e

    if not isinstance(raw, dict):
        raise MalformedOverridesError(
            f"Search overrides file {path} must be a JSON object (got {type(raw).__name__})."
        )

    overrides: dict[str, ResolvedOverride] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            logger.warning(f"Skipping search override with invalid key: {key!r}")
            continue
        key = key.strip()

        if isinstance(value, str):
            search_term: str | None = value
            weight_g: float | None = None
        elif isinstance(value, dict):
            search_term = value.get("search_term")
            weight_g = _parse_weight(key, value.get("our_weight_g"))
        else:
            logger.warning(
                f"Skipping search override {key!r}: value must be a string or object."
            )
            continue

        if not isinstance(search_term, str) or not search_term.strip():
            logger.warning(
                f"Skipping search override {key!r}: missing non-empty 'search_term'."
            )
            continue

        overrides[key] = ResolvedOverride(search_term=search_term, weight_g=weight_g)

    return overrides


def _parse_weight(key: str, raw_weight) -> float | None:
    """Validate an object-form ``our_weight_g``; warn and drop it if not a number."""
    if raw_weight is None:
        return None
    # bool is a subclass of int -- reject it explicitly.
    if isinstance(raw_weight, bool) or not isinstance(raw_weight, (int, float)):
        logger.warning(
            f"Search override {key!r}: 'our_weight_g' must be a number; ignoring weight "
            f"{raw_weight!r}. This item will have no manual weight, so unit conversion may "
            f"be skipped."
        )
        return None
    return float(raw_weight)


def resolve_search_overrides(
    items: Iterable["OfferPart"],
    overrides: dict[str, ResolvedOverride] | None = None,
    log: bool = True,
) -> dict[int, ResolvedOverride]:
    """
    Resolve which items are overridden by prefix-matching each override key against item
    codes (SQL ``code LIKE 'key%'``).

    Args:
        items: Iterable of objects with ``.id`` (int) and ``.code`` (str) attributes,
            in the order they were fetched (``ORDER BY id``).
        overrides: Pre-loaded overrides; defaults to ``load_search_overrides()``.
        log: Emit warnings for blank codes / multi-match / unmatched keys. Set ``False`` when
            recomputing for display (e.g. result markers) so run-time warnings aren't repeated.

    Returns:
        Mapping of item_id -> ResolvedOverride for matched items. If a key matches more than
        one item, a warning is logged and only the first match (in ``items`` order) is used.
        Keys that match nothing, or a configuration where no item carries a code, are warned
        about rather than silently ignored.
    """
    if overrides is None:
        overrides = load_search_overrides()
    if not overrides:
        return {}

    item_list = list(items)

    # Overrides configured but no item carries a code -> every key would match nothing.
    # Warn distinctly so this reads as "codes aren't being fetched", not "your keys are wrong".
    if item_list and not any((getattr(item, "code", "") or "") for item in item_list):
        if log:
            logger.warning(
                "Search overrides are configured but no items have a 'code' value -- check the "
                "FETCH_ITEMS query / code column. No overrides will be applied."
            )
        return {}

    resolved: dict[int, ResolvedOverride] = {}
    unmatched: list[str] = []

    for key, entry in overrides.items():
        matches = [item for item in item_list if (getattr(item, "code", "") or "").startswith(key)]
        if not matches:
            unmatched.append(key)
            continue
        if len(matches) > 1 and log:
            logger.warning(
                f"Search override {key!r} matched {len(matches)} item codes "
                f"({[m.code for m in matches]}); applying to first only ({matches[0].code})."
            )
        resolved[matches[0].id] = entry

    if unmatched and log:
        logger.warning(
            f"Search overrides with no matching item code this run: {unmatched}; "
            "check for typos or stale entries."
        )

    return resolved


def apply_search_overrides(
    resolved: dict[int, ResolvedOverride],
    search_terms: dict[int, str],
    weights: dict[int, float | None],
) -> None:
    """
    Merge resolved overrides into the search-term and weight maps **in place**.

    For each overridden item the override search term replaces any generated term, and a
    manual weight (when provided) replaces any generated weight. Logs a single summary line.
    Shared by both the wizard and CLI flows so the merge logic lives in one place.
    """
    if not resolved:
        return
    for item_id, ov in resolved.items():
        search_terms[item_id] = ov.search_term
        if ov.weight_g is not None:
            weights[item_id] = ov.weight_g
    logger.info(f"Applied {len(resolved)} search overrides")


def overridden_item_ids(items: Iterable["OfferPart"], path: Path | None = None) -> set[int]:
    """
    Recompute which item ids are search-term-overridden, for display markers.

    Re-reads ``search_overrides.json`` and re-runs matching **quietly** (the run-time warnings
    already fired). Returns an empty set if the file is absent or malformed -- a marker is
    cosmetic, so it must never raise or block report/TUI rendering.
    """
    try:
        loaded = load_search_overrides(path)
    except MalformedOverridesError:
        return set()
    return set(resolve_search_overrides(items, overrides=loaded, log=False))
