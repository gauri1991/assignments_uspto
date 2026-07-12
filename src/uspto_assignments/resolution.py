"""The entity-resolution cascade: exact hash → person detector → capped blocked fuzzy → provisional.

Resolution runs once per **distinct** mention. The cascade is ordered so the O(1) exact lookup
resolves the bulk and fuzzy only ever sees the residual; the fuzzy index uses **capped prefix
blocks** (a block over the cap is re-split on a longer prefix) so no pathological prefix — think
``THE …`` or ``INTE…`` — degrades to a full scan. Unresolved organizations are clustered against
each other and minted **stable provisional ids**, so off-gazetteer buyers under many spellings
collapse to one entity and keep the same id across runs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha1

from rapidfuzz import process

from .classify import classify_name
from .dictionary import ResolutionDictionary, provisional_id, strip_legal_suffix
from .normalize import DEFAULT_THRESHOLD, CappedBlockIndex, clean, get_scorer

# The cascade caps fuzzy blocks tighter than the gazetteer default: it runs per-mention over the
# whole 516k-key dictionary, so a smaller cap keeps each probe cheap (results unchanged vs the
# prior cascade, which used this same value).
_BLOCK_CAP = 2000
_CLUSTER_THRESHOLD = 95  # provisional-vs-provisional clustering is stricter than dictionary match

OnProgress = Callable[[int, int], None]
_PROGRESS_EVERY = 2000


@dataclass(slots=True)
class ResolvedMention:
    """The resolution of one distinct raw mention string."""

    mention: str
    entity_id: str
    canonical_name: str
    entity_type: str  # company | individual | unknown
    resolution_source: str  # exact | fuzzy | person | provisional | unresolved
    resolution_confidence: float
    ultimate_parent_id: str


def _person_mention(mention: str, key: str) -> ResolvedMention:
    entity_id = "person-" + sha1(key.encode("utf-8")).hexdigest()[:16]
    return ResolvedMention(
        mention=mention,
        entity_id=entity_id,
        canonical_name=key.title(),
        entity_type="individual",
        resolution_source="person",
        resolution_confidence=0.95,
        ultimate_parent_id=entity_id,
    )


def _resolved_from_entity(
    mention: str, entity_id: str, dictionary: ResolutionDictionary, source: str, confidence: float
) -> ResolvedMention:
    attrs = dictionary.entity(entity_id)
    return ResolvedMention(
        mention=mention,
        entity_id=entity_id,
        canonical_name=attrs.get("canonical_name", mention),
        entity_type=attrs.get("entity_type", "company"),
        resolution_source=source,
        resolution_confidence=confidence,
        ultimate_parent_id=attrs.get("ultimate_parent_id") or entity_id,
    )


def _cluster_residual(residual: dict[str, str], scorer_name: str) -> dict[str, tuple[str, str]]:
    """Cluster unresolved org keys against each other; return ``key -> (prov_id, canonical)``.

    Two-stage union-find: keys sharing the same **suffix-stripped** form merge exactly
    (``… INC`` == ``… INCORPORATED``), then distinct stripped forms fuzzy-merge within capped
    blocks at the strict cluster threshold. The representative is the lexicographically smallest
    key, so ids are deterministic for a given input set.
    """
    keys = sorted(residual)
    parent: dict[str, str] = {k: k for k in keys}

    def find(k: str) -> str:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:  # keep the lexicographically smaller as root -> deterministic representative
            low, high = (ra, rb) if ra < rb else (rb, ra)
            parent[high] = low

    # Stage 1: exact merge on the suffix-stripped form (legal-form variants collapse for free).
    by_stripped: dict[str, str] = {}
    for key in keys:
        stripped = strip_legal_suffix(key) or key
        if stripped in by_stripped:
            union(key, by_stripped[stripped])
        else:
            by_stripped[stripped] = key

    # Stage 2: fuzzy merge across the distinct stripped forms, within capped blocks.
    forms = sorted(by_stripped)
    index: CappedBlockIndex[str] = CappedBlockIndex(forms, cap=_BLOCK_CAP)
    scorer = get_scorer(scorer_name)
    for form in forms:
        block = [c for c in index.candidates(form) if c > form]  # each unordered pair scored once
        if not block:
            continue
        for match in process.extract(
            form, block, scorer=scorer, score_cutoff=_CLUSTER_THRESHOLD, limit=5
        ):
            union(by_stripped[form], by_stripped[match[0]])

    out: dict[str, tuple[str, str]] = {}
    for key in keys:
        rep = find(key)
        out[key] = (provisional_id(rep), residual[rep])
    return out


def resolve_mentions(
    mentions: list[str],
    dictionary: ResolutionDictionary,
    *,
    threshold: int = DEFAULT_THRESHOLD,
    scorer: str = "token_set",
    on_progress: OnProgress | None = None,
) -> tuple[dict[str, ResolvedMention], list[dict[str, str]]]:
    """Resolve distinct mention strings; returns ``(mention -> resolution, new provisional rows)``.

    Cascade per distinct mention: clean → **exact** alias lookup (incl. suffix-stripped key) →
    **person** detector → **capped blocked fuzzy** against dictionary canonicals → **provisional**
    minting with residual-vs-residual clustering. The provisional rows should be persisted via
    :func:`uspto_assignments.dictionary.append_provisionals` so ids stay stable across runs.
    """
    distinct = sorted({m for m in mentions if m and m.strip()})
    fuzzy_index: CappedBlockIndex[str] = CappedBlockIndex(dictionary.canonical_keys, cap=_BLOCK_CAP)
    scorer_fn = get_scorer(scorer)

    resolved: dict[str, ResolvedMention] = {}
    residual: dict[str, str] = {}  # cleaned key -> a representative raw mention
    for done, mention in enumerate(distinct, start=1):
        key = clean(mention)
        if not key:
            resolved[mention] = ResolvedMention(
                mention, "", mention, "unknown", "unresolved", 0.0, ""
            )
            continue
        exact = dictionary.lookup_exact(key)
        if exact is not None:
            resolved[mention] = _resolved_from_entity(mention, exact, dictionary, "exact", 1.0)
            continue
        if classify_name(mention) == "individual":
            resolved[mention] = _person_mention(mention, key)
            continue
        block = fuzzy_index.candidates(key)
        match = (
            process.extractOne(key, block, scorer=scorer_fn, score_cutoff=threshold)
            if block
            else None
        )
        if match is not None:
            entity_id = dictionary.key_to_entity[match[0]]
            resolved[mention] = _resolved_from_entity(
                mention, entity_id, dictionary, "fuzzy", float(match[1]) / 100.0
            )
            continue
        residual.setdefault(key, mention)
        if on_progress is not None and done % _PROGRESS_EVERY == 0:
            on_progress(done, len(distinct))

    provisional_rows: list[dict[str, str]] = []
    if residual:
        clusters = _cluster_residual(residual, scorer)
        minted: set[str] = set()
        for mention in distinct:
            if mention in resolved:
                continue
            key = clean(mention)
            prov_id, canonical = clusters[key]
            entity_type = "company" if classify_name(canonical) == "company" else "unknown"
            resolved[mention] = ResolvedMention(
                mention=mention,
                entity_id=prov_id,
                canonical_name=canonical,
                entity_type=entity_type,
                resolution_source="provisional",
                resolution_confidence=0.5,
                ultimate_parent_id=prov_id,
            )
            if key not in minted:
                minted.add(key)
                provisional_rows.append(
                    {
                        "alias_key": key,
                        "entity_id": prov_id,
                        "canonical_name": canonical,
                        "entity_type": entity_type,
                    }
                )
    if on_progress is not None:
        on_progress(len(distinct), len(distinct))
    return resolved, provisional_rows


def split_parties(value: str | None, separator: str = "; ") -> list[str]:
    """Split a concatenated multi-party ``*_names`` value into individual party mentions."""
    if not value:
        return []
    return [part.strip() for part in value.split(separator) if part.strip()]


def suffix_stripped_key(name: str) -> str:
    """The cleaned, legal-suffix-stripped key for a raw name (exposed for tests/diagnostics)."""
    return strip_legal_suffix(clean(name))
