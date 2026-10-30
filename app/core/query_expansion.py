"""Deterministic, auditable keyword query transformations (no model inference)."""

from __future__ import annotations

import re

from app.core.topic import validate_topic

EXPANSION_VERSION = "keyword-rules-v1"
MAX_EXPANSIONS = 4

# One-way alternatives are intentionally narrow and easy to review. They are not
# relevance claims; each transformation is a lexical search variation.
SYNONYM_RULES = (
    ("prediction", "forecasting"),
    ("forecasting", "prediction"),
    ("modeling", "modelling"),
    ("modelling", "modeling"),
)
ABBREVIATION_RULES = {
    "AI": "artificial intelligence",
    "GIS": "geographic information systems",
    "ML": "machine learning",
    "NLP": "natural language processing",
}
METHOD_TERMS = frozenset({
    "algorithm", "algorithms", "approach", "approaches", "classification",
    "detection", "forecasting", "learning", "method", "methods", "model",
    "models", "modeling", "modelling", "prediction", "regression", "simulation",
})


def _replace_phrase(text: str, source: str, target: str) -> str | None:
    pattern = re.compile(rf"(?<!\w){re.escape(source)}(?!\w)", re.IGNORECASE)
    result, count = pattern.subn(target, text, count=1)
    return " ".join(result.split()) if count else None


def _variants(topic: str) -> list[tuple[str, str]]:
    """Apply synonym, abbreviation, syntactic, then conservative filler rules."""
    candidates: list[tuple[str, str]] = []
    for source, target in SYNONYM_RULES:
        if result := _replace_phrase(topic, source, target):
            candidates.append((result, f"synonym: {source} -> {target}"))
    for abbreviation, expansion in ABBREVIATION_RULES.items():
        # Requiring uppercase prevents ordinary short words such as "ai" from
        # being interpreted as specialist abbreviations.
        pattern = re.compile(rf"(?<!\w){re.escape(abbreviation)}(?!\w)")
        result, count = pattern.subn(expansion, topic, count=1)
        if count:
            candidates.append((" ".join(result.split()), f"abbreviation: {abbreviation} -> {expansion}"))

    # Rephrase only an explicit single "X for Y" construction. This retains all
    # topic terms and changes their connective phrasing in a predictable way.
    match = re.fullmatch(r"\s*(.+?)\s+for\s+(.+?)\s*", topic, flags=re.IGNORECASE)
    method_relation = False
    if match:
        left_terms = match.group(1).split()
        method_relation = bool(left_terms and left_terms[-1].casefold() in METHOD_TERMS)
    if match and method_relation:
        candidates.append((f"{match.group(2)} using {match.group(1)}", "method-for-task phrasing"))

    # Drop a leading "study of" or "research on" only; these are framing
    # phrases and the complete subject remains unchanged.
    candidates.extend((
        (re.sub(r"^study\s+of\s+", "", topic, flags=re.IGNORECASE), "remove framing: study of"),
        (re.sub(r"^research\s+on\s+", "", topic, flags=re.IGNORECASE), "remove framing: research on"),
    ))

    # Compose a directly evidenced lexical synonym with the safe "for" removal
    # so the example topic can yield "machine learning flood forecasting".
    no_for = re.sub(r"\s+for\s+", " ", topic, count=1, flags=re.IGNORECASE)
    if match and method_relation and no_for != topic:
        for source, target in SYNONYM_RULES:
            if result := _replace_phrase(no_for, source, target):
                candidates.append((result, f"remove for + synonym: {source} -> {target}"))
    return candidates


def expand_query_details(topic: str, *, limit: int = MAX_EXPANSIONS) -> list[dict[str, str]]:
    """Return ordered query strings and their transparent transformation rules."""
    original = validate_topic(topic)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= MAX_EXPANSIONS:
        raise ValueError(f"expansion limit must be between 0 and {MAX_EXPANSIONS}")
    queries = [{"query": original, "rule": "original topic"}]
    if limit == 0:
        return queries
    seen = {" ".join(original.split()).casefold()}
    for candidate, rule in _variants(original):
        normalized = " ".join(candidate.split())
        key = normalized.casefold()
        if normalized and key not in seen:
            seen.add(key)
            queries.append({"query": normalized, "rule": rule})
            if len(queries) - 1 >= limit:
                break
    return queries


def expand_queries(topic: str, *, limit: int = MAX_EXPANSIONS) -> list[str]:
    """Return original validated topic plus up to ``limit`` stable variations.

    The original validated topic is always first and unchanged. ``limit`` counts
    generated variations, not the original query; zero requests only the original.
    Exact/case-insensitive duplicate candidates are removed in rule order.
    """
    return [item["query"] for item in expand_query_details(topic, limit=limit)]
