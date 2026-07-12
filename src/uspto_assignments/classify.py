"""Entity-type classification: label an assignor/assignee name as a company or an individual.

The default classifier is **rule-based** — companies are detected by legal-form and organization
keywords (near-universal in USPTO data), individuals by the dominant ``LAST, FIRST`` inventor
format; genuinely ambiguous names (e.g. single-token brands) are left ``"unknown"`` rather than
guessed. An optional ``probablepeople`` ML backend refines the ambiguous tail when installed.

:func:`classify_column` adds a type column, classifying only the **distinct** values (dictionary
encoding) so cost scales with unique names, not row count — the same pattern as
:func:`uspto_assignments.normalize.normalize_column`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Literal

import pyarrow as pa
import pyarrow.compute as _pc_module

from .normalize import clean

# pyarrow.compute is under-typed in pyarrow-stubs; route through Any (see filters.py for rationale).
pc: Any = _pc_module

logger = logging.getLogger(__name__)

EntityType = Literal["company", "individual", "unknown"]
ClassifyMethod = Literal["rules", "probablepeople"]
CombineMode = Literal["all", "any", "first", "majority"]

OnProgress = Callable[[int, int], None]
_PROGRESS_EVERY = 500

# Legal forms and organization keywords. Presence of any (after cleaning) marks a name as a company;
# this is highly reliable for USPTO assignees/assignors, which carry explicit legal suffixes.
_COMPANY_TOKENS: frozenset[str] = frozenset(
    {
        # legal forms
        "INC",
        "INCORPORATED",
        "CORP",
        "CORPORATION",
        "CO",
        "COMPANY",
        "LLC",
        "LLP",
        "LP",
        "LTD",
        "LIMITED",
        "PLC",
        "GMBH",
        "AG",
        "KG",
        "SA",
        "SARL",
        "SAS",
        "NV",
        "BV",
        "OY",
        "AB",
        "AS",
        "SPA",
        "SRL",
        "KK",
        "PTY",
        "PTE",
        "ULC",
        "LC",
        "OYJ",
        "ASA",
        "APS",
        # organization keywords
        "TRUST",
        "BANK",
        "UNIVERSITY",
        "INSTITUTE",
        "FOUNDATION",
        "HOLDINGS",
        "HOLDING",
        "GROUP",
        "TECHNOLOGIES",
        "TECHNOLOGY",
        "SYSTEMS",
        "SOLUTIONS",
        "LABORATORIES",
        "LABS",
        "SEMICONDUCTOR",
        "PHARMACEUTICALS",
        "PHARMACEUTICAL",
        "PHARMA",
        "ELECTRONICS",
        "COMMUNICATIONS",
        "NETWORKS",
        "INDUSTRIES",
        "INDUSTRIAL",
        "INTERNATIONAL",
        "ENTERPRISES",
        "ENTERPRISE",
        "PARTNERS",
        "ASSOCIATES",
        "MANUFACTURING",
        "PRODUCTS",
        "DEVICES",
        "MEDICAL",
        "HEALTHCARE",
        "ENERGY",
        "MOTORS",
        "CAPITAL",
        "VENTURES",
        "INSURANCE",
        "RESEARCH",
        "SERVICES",
        "CONSULTING",
        "DIAGNOSTICS",
        "BIOSCIENCES",
        "THERAPEUTICS",
        "AKTIENGESELLSCHAFT",
    }
)
# Multi-token company phrases (checked as adjacent tokens).
_COMPANY_PHRASES: tuple[tuple[str, ...], ...] = (("KABUSHIKI", "KAISHA"),)
# Personal-name suffixes — a weak individual signal.
_PERSON_SUFFIXES: frozenset[str] = frozenset({"JR", "SR", "II", "III", "IV", "MD", "PHD", "ESQ"})

_MIN_PERSON_TOKENS = 2
_MAX_PERSON_TOKENS = 4
_MAX_NAME_PART_TOKENS = 3  # each side of "LAST, FIRST"


def _tokens(cleaned: str) -> list[str]:
    return cleaned.split()


def _has_company_signal(cleaned: str, tokens: list[str]) -> bool:
    if any(token in _COMPANY_TOKENS for token in tokens):
        return True
    return any(phrase[0] in tokens and " ".join(phrase) in cleaned for phrase in _COMPANY_PHRASES)


def _looks_like_person(name: str, cleaned: str, tokens: list[str]) -> bool:
    """True for a ``LAST, FIRST`` comma form or a short all-alpha personal name."""
    if not tokens:
        return False
    core = [t for t in tokens if t not in _PERSON_SUFFIXES]
    if "," in name:  # "LAST, FIRST [MIDDLE]" — the dominant USPTO inventor format
        left, _, right = name.partition(",")
        left_tokens = _tokens(clean(left))
        right_tokens = _tokens(clean(right))
        if (
            1 <= len(left_tokens) <= _MAX_NAME_PART_TOKENS
            and 1 <= len(right_tokens) <= _MAX_NAME_PART_TOKENS
        ):
            return True
    # a short, all-alphabetic name with no organization keyword reads as a person
    return _MIN_PERSON_TOKENS <= len(core) <= _MAX_PERSON_TOKENS and all(
        token.isalpha() for token in core
    )


def _classify_rules(name: str) -> EntityType:
    cleaned = clean(name)
    if not cleaned:
        return "unknown"
    tokens = _tokens(cleaned)
    if _has_company_signal(cleaned, tokens):  # company keywords win over person heuristics
        return "company"
    if _looks_like_person(name, cleaned, tokens):
        return "individual"
    return "unknown"


def _probablepeople_classify(name: str) -> EntityType:
    """Classify via the optional ``probablepeople`` CRF model, falling back to rules if absent."""
    try:
        import probablepeople  # type: ignore[import-untyped]  # noqa: PLC0415 - optional backend
    except ImportError:
        logger.warning("probablepeople not installed; falling back to rule-based classification")
        return _classify_rules(name)
    pp: Any = probablepeople  # under-typed third-party module
    try:
        _tagged, name_type = pp.tag(name)
    except Exception:  # probablepeople raises on unparseable input — treat as ambiguous
        return _classify_rules(name)
    if name_type == "Corporation":
        return "company"
    if name_type == "Person":
        return "individual"
    return _classify_rules(name)


def classify_name(name: str, *, method: ClassifyMethod = "rules") -> EntityType:
    """Classify a single name as ``"company"``, ``"individual"``, or ``"unknown"``."""
    if method == "probablepeople":
        return _probablepeople_classify(name)
    return _classify_rules(name)


def _combine(types: list[EntityType], mode: CombineMode) -> EntityType:  # noqa: PLR0911 - per mode
    """Reduce the per-party types of a multi-party value to one type by ``mode``."""
    if not types:
        return "unknown"
    if mode == "first":
        return types[0]
    if mode == "any":  # company if any party is a company, else individual if any, else unknown
        if "company" in types:
            return "company"
        return "individual" if "individual" in types else "unknown"
    if mode == "majority":
        company = types.count("company")
        individual = types.count("individual")
        if company > individual:
            return "company"
        if individual > company:
            return "individual"
        return "unknown"
    # "all": a single agreed type across every party, else unknown
    unique = set(types)
    return types[0] if len(unique) == 1 else "unknown"


def classify_value(
    value: str, *, method: ClassifyMethod = "rules", separator: str = "", mode: CombineMode = "all"
) -> EntityType:
    """Classify one column value; split concatenated multi-party names when ``separator`` is set."""
    if not separator:
        return classify_name(value, method=method)
    parts = [p.strip() for p in value.split(separator) if p.strip()]
    if not parts:
        return "unknown"
    return _combine([classify_name(p, method=method) for p in parts], mode)


def classify_column(  # noqa: PLR0913 - a clear public entry point with keyword-only options
    table: pa.Table,
    column: str,
    target: str,
    *,
    method: ClassifyMethod = "rules",
    separator: str = "",
    mode: CombineMode = "all",
    on_progress: OnProgress | None = None,
) -> pa.Table:
    """Return ``table`` with a ``target`` column of entity types for ``column``.

    Classifies once per **distinct** value (dictionary-encoded) then maps back over all rows, so the
    cost scales with unique names. When ``separator`` is set, multi-party values are split and
    combined by ``mode``. Calls ``on_progress(done, total)`` as distinct values are classified.
    """
    # Route the column through Any (pyarrow-stubs under-type dictionary_encode / take).
    source: Any = table.column(column).combine_chunks()
    encoded: Any = source.dictionary_encode()
    distinct: list[Any] = encoded.dictionary.to_pylist()
    total = len(distinct)
    mapped: list[str | None] = []
    for index, value in enumerate(distinct):
        mapped.append(
            None
            if value is None
            else classify_value(value, method=method, separator=separator, mode=mode)
        )
        if on_progress is not None and (index + 1) % _PROGRESS_EVERY == 0:
            on_progress(index + 1, total)
    if on_progress is not None:
        on_progress(total, total)

    target_array: Any = pc.take(pa.array(mapped, type=pa.string()), encoded.indices)
    if target in table.column_names:
        table = table.drop_columns([target])
    return table.append_column(target, target_array)
