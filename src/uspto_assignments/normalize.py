"""Entity-name normalization: fuzzy-match messy assignor/assignee names to canonical forms.

An :class:`EntityMemory` holds canonical names plus learned alias→canonical mappings; it seeds
from a file, grows as names are resolved, and persists to JSON. :func:`normalize_column` adds a
canonical column, matching only the **distinct** values (rapidfuzz) so cost scales with unique
names, not row count.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.compute as _pc_module
from rapidfuzz import fuzz, process
from rapidfuzz.distance import JaroWinkler

# pyarrow.compute is under-typed in pyarrow-stubs; route through Any (see filters.py for rationale).
pc: Any = _pc_module

logger = logging.getLogger(__name__)

_NON_ALNUM = re.compile(r"[^A-Z0-9]+")
DEFAULT_THRESHOLD = 90
_PROGRESS_EVERY = 500

# Fuzzy candidates are blocked by the first few cleaned-name characters, so a query only compares
# against canonicals sharing that prefix — O(block) instead of O(all canonicals). A single fixed
# prefix, though, lets one prefix accumulate an unbounded block (a real gazetteer has ~7k orgs
# behind ``THE ``); ``CappedBlockIndex`` re-splits any block over ``_BLOCK_CAP`` onto a longer
# prefix so the block a fuzzy probe scans stays bounded no matter how skewed the distribution.
_BLOCK_BASE = 4  # first bucketing prefix length
_BLOCK_STEP = 2  # extend the prefix by this much when a block overflows
_BLOCK_MAX = 8  # never extend the prefix beyond this many characters
# Default chosen ABOVE the largest natural block in the real 516k PatentsView gazetteer (``THE `` ≈
# 7,265) so real-world blocking is byte-for-byte unchanged; only a pathological prefix re-splits.
_BLOCK_CAP = 10_000

OnProgress = Callable[[int, int], None]


def _jaro_winkler(query: str, choice: str, *, score_cutoff: float = 0.0) -> float:
    """Jaro-Winkler similarity scaled to rapidfuzz's 0–100 convention."""
    return JaroWinkler.normalized_similarity(query, choice) * 100.0


# Selectable rapidfuzz scorers for fuzzy matching. ``wratio`` is the default (weighted ratio, robust
# to length/word differences); ``token_set``/``token_sort`` shine on reordered/extra company words.
_SCORERS: dict[str, Callable[..., float]] = {
    "wratio": fuzz.WRatio,
    "token_set": fuzz.token_set_ratio,
    "token_sort": fuzz.token_sort_ratio,
    "partial": fuzz.partial_ratio,
    "qratio": fuzz.QRatio,
    "ratio": fuzz.ratio,
    "jaro_winkler": _jaro_winkler,
}
DEFAULT_SCORER = "wratio"


def scorer_names() -> list[str]:
    """The names of the available fuzzy scorers (for UI dropdowns)."""
    return list(_SCORERS)


def get_scorer(name: str) -> Callable[..., float]:
    """Return the rapidfuzz scorer for ``name`` (falls back to WRatio if unknown)."""
    return _SCORERS.get(name, fuzz.WRatio)


def clean(name: str) -> str:
    """Return an uppercased, punctuation-stripped, whitespace-collapsed match key."""
    return _NON_ALNUM.sub(" ", name.upper()).strip()


class _BlockNode[T]:
    """One prefix block: a leaf list until it overflows ``cap``, then a dict of longer-prefix nodes.

    Re-splitting fully replaces a leaf with its children (the parent list is emptied), and lookups
    always descend children first, so a key is reachable in exactly one block — no shadowing.
    """

    __slots__ = ("_cap", "_children", "_items", "_length")

    def __init__(self, length: int, cap: int) -> None:
        self._length = length
        self._cap = cap
        self._items: list[tuple[str, T]] = []
        self._children: dict[str, _BlockNode[T]] | None = None

    def add(self, key: str, payload: T) -> None:
        if self._children is not None:
            self._child(key).add(key, payload)
            return
        self._items.append((key, payload))
        if len(self._items) > self._cap and self._length < _BLOCK_MAX:
            items, self._items = self._items, []
            self._children = {}
            for existing_key, existing_payload in items:
                self._child(existing_key).add(existing_key, existing_payload)

    def _child(self, key: str) -> _BlockNode[T]:
        assert self._children is not None
        prefix = key[: self._length + _BLOCK_STEP]
        child = self._children.get(prefix)
        if child is None:
            child = _BlockNode[T](self._length + _BLOCK_STEP, self._cap)
            self._children[prefix] = child
        return child

    def candidates(self, key: str) -> list[T]:
        if self._children is not None:
            child = self._children.get(key[: self._length + _BLOCK_STEP])
            return child.candidates(key) if child is not None else []
        return [payload for _, payload in self._items]

    def max_block(self) -> int:
        if self._children is not None:
            return max((c.max_block() for c in self._children.values()), default=0)
        return len(self._items)

    def block_sizes(self, out: list[int]) -> None:
        if self._children is None:
            out.append(len(self._items))
            return
        for child in self._children.values():
            child.block_sizes(out)


class CappedBlockIndex[T]:
    """A prefix-block index whose oversized blocks re-split onto a longer prefix.

    Buckets by the first ``_BLOCK_BASE`` cleaned characters; any bucket exceeding ``cap`` re-splits
    onto a prefix ``_BLOCK_STEP`` chars longer, up to ``_BLOCK_MAX`` (identical keys beyond that are
    kept together). Below the cap a bucket is a plain insertion-ordered list, so results match a
    fixed-prefix index exactly. Shared by :class:`EntityMemory` (payload = canonical index) and the
    resolution cascade (payload = the key itself); ``add`` is incremental so the built-once
    gazetteer and the growing learn-memory use one implementation.
    """

    __slots__ = ("_cap", "_root")

    def __init__(self, keys: Iterable[str] | None = None, *, cap: int = _BLOCK_CAP) -> None:
        self._cap = cap
        self._root: dict[str, _BlockNode[T]] = {}
        for key in keys or []:
            self.add(key, cast("T", key))  # str convenience: payload = the key itself

    def add(self, key: str, payload: T) -> None:
        """Index ``payload`` under ``key``'s prefix block (re-splitting it if it overflows)."""
        prefix = key[:_BLOCK_BASE]
        node = self._root.get(prefix)
        if node is None:
            node = _BlockNode[T](_BLOCK_BASE, self._cap)
            self._root[prefix] = node
        node.add(key, payload)

    def candidates(self, key: str) -> list[T]:
        """The payloads sharing ``key``'s (possibly re-split) prefix block."""
        node = self._root.get(key[:_BLOCK_BASE])
        return node.candidates(key) if node is not None else []

    def max_block(self) -> int:
        """Size of the largest leaf block (bounded by ``cap`` unless keys are identical)."""
        return max((n.max_block() for n in self._root.values()), default=0)

    def block_sizes(self) -> list[int]:
        """All leaf-block sizes (for ``-v`` block-distribution logging)."""
        out: list[int] = []
        for node in self._root.values():
            node.block_sizes(out)
        return out


class EntityMemory:
    """Canonical names + learned aliases, with fuzzy resolution and JSON persistence."""

    def __init__(
        self,
        canonicals: list[str] | None = None,
        aliases: dict[str, str] | None = None,
        types: dict[str, str] | None = None,
    ) -> None:
        self._canonicals: list[str] = []
        self._canon_keys: list[str] = []
        self._canon_set: set[str] = set()
        # cleaned-name prefix block -> canonical indices, capped/re-split so no single prefix
        # (e.g. the gazetteer's ~7k ``THE …`` orgs) degrades a fuzzy probe to a full scan.
        self._block_index: CappedBlockIndex[int] = CappedBlockIndex(cap=_BLOCK_CAP)
        self._aliases: dict[str, str] = {}
        # Fuzzy score at learn time, keyed like _aliases. Missing key = 100 (exact/curated), so
        # only fuzzy-learned aliases carry an entry and the JSON stays compact.
        self._alias_scores: dict[str, int] = {}
        # Optional per-canonical entity type ("company"/"individual"/"unknown"), keyed by canonical
        # NAME (not index) so it survives _rebuild(). Absent key = untagged. A plain str, not
        # classify's EntityType, because classify imports this module (would be a circular import).
        self._types: dict[str, str] = {}
        self._learned: list[tuple[str, str, int]] = []
        for name in canonicals or []:
            self._add_canonical(name)
        for alias_key, canonical in (aliases or {}).items():
            self._add_canonical(canonical)
            self._aliases[alias_key] = canonical
        for name, entity_type in (types or {}).items():
            if name in self._canon_set and entity_type:
                self._types[name] = entity_type

    # -- reads --------------------------------------------------------------
    @property
    def canonicals(self) -> list[str]:
        return list(self._canonicals)

    @property
    def aliases(self) -> dict[str, str]:
        return dict(self._aliases)

    def alias_score(self, alias_key: str) -> int:
        """Confidence (0–100) recorded when ``alias_key`` was learned; 100 = exact or curated."""
        return self._alias_scores.get(alias_key, 100)

    @property
    def learned(self) -> list[tuple[str, str, int]]:
        """``(alias, canonical, score)`` recorded since construction (merged back after a batch)."""
        return list(self._learned)

    def counts(self) -> tuple[int, int]:
        """Return ``(canonical_count, alias_count)``."""
        return len(self._canonicals), len(self._aliases)

    @property
    def types(self) -> dict[str, str]:
        """Per-canonical entity type map (canonical name → type); only tagged entities appear."""
        return dict(self._types)

    def entity_type(self, name: str) -> str | None:
        """The stored entity type for canonical ``name`` (``None`` when untagged)."""
        return self._types.get(name)

    def set_type(self, name: str, entity_type: str) -> None:
        """Tag canonical ``name`` with ``entity_type`` (no-op if the canonical is unknown)."""
        if name in self._canon_set and entity_type:
            self._types[name] = entity_type

    # -- mutation -----------------------------------------------------------
    def _add_canonical(self, name: str) -> None:
        if name and name not in self._canon_set:
            index = len(self._canonicals)
            key = clean(name)
            self._canonicals.append(name)
            self._canon_keys.append(key)
            self._canon_set.add(name)
            self._block_index.add(key, index)

    def _fuzzy_match(self, key: str, threshold: int, scorer: str) -> tuple[str, int] | None:
        """Best ``(canonical, score)`` for ``key`` within its block; None below ``threshold``."""
        indices = self._block_index.candidates(key)
        if not indices:
            return None
        choices = [self._canon_keys[i] for i in indices]
        scorer_fn = _SCORERS.get(scorer, fuzz.WRatio)
        match = process.extractOne(key, choices, scorer=scorer_fn, score_cutoff=threshold)
        if match is None:
            return None
        return self._canonicals[indices[match[2]]], round(match[1])

    def max_block(self) -> int:
        """Largest fuzzy block (diagnostics): bounded by the cap unless names are identical."""
        return self._block_index.max_block()

    def resolve(
        self,
        name: str,
        *,
        threshold: int = DEFAULT_THRESHOLD,
        learn: bool = True,
        scorer: str = DEFAULT_SCORER,
    ) -> tuple[str, bool, int]:
        """Resolve ``name`` to a canonical form.

        Returns ``(canonical, is_new, score)``. Exact alias hits are O(1) and report the score the
        alias was originally learned with (100 for curated/exact ones), so a marginal fuzzy match
        stays visible on later runs. Otherwise the cleaned key is fuzzy-matched (rapidfuzz
        ``scorer``, ``score_cutoff=threshold``) against canonicals **sharing its prefix block**.
        With ``learn=True`` an unmatched name becomes a new canonical (score 100 — identity); with
        ``learn=False`` it is returned as its cleaned form with score 0 (no match).
        """
        key = clean(name)
        if not key:
            return name, False, 0
        existing = self._aliases.get(key)
        if existing is not None:
            return existing, False, self.alias_score(key)
        match = self._fuzzy_match(key, threshold, scorer)
        if match is not None:
            canonical, score = match
            self._aliases[key] = canonical
            if score < 100:
                self._alias_scores[key] = score
            self._learned.append((key, canonical, score))
            return canonical, False, score
        if not learn:
            return key, False, 0  # match-only: leave unmatched as-is, add nothing
        self._add_canonical(key)  # new entity: the cleaned name is its canonical form
        self._aliases[key] = key
        self._learned.append((key, key, 100))
        return key, True, 100

    def match(
        self, name: str, *, threshold: int = DEFAULT_THRESHOLD, scorer: str = DEFAULT_SCORER
    ) -> tuple[str, int] | None:
        """Return ``(canonical, score)`` for ``name`` if known/close, else ``None`` — no mutation.

        Unlike :meth:`resolve`, this never learns and returns ``None`` on no match, so callers can
        distinguish "found in a curated reference" from "unmatched" (e.g. gazetteer matching).
        Exact alias hits report the score the alias was learned with (100 for curated ones).
        """
        key = clean(name)
        if not key:
            return None
        existing = self._aliases.get(key)
        if existing is not None:
            return existing, self.alias_score(key)
        return self._fuzzy_match(key, threshold, scorer)

    def apply_learned(self, pairs: list[tuple[str, str, int]]) -> None:
        """Merge ``(alias, canonical, score)`` learned elsewhere (e.g. by a worker process)."""
        for alias_key, canonical, score in pairs:
            self._add_canonical(canonical)
            self._aliases[alias_key] = canonical
            if score < 100:
                self._alias_scores[alias_key] = score

    def merge(self, other: EntityMemory) -> None:
        """Absorb another memory's canonicals, aliases, learn-time scores, and type tags."""
        for name in other._canonicals:
            self._add_canonical(name)
        for alias_key, canonical in other._aliases.items():
            self._add_canonical(canonical)
            self._aliases[alias_key] = canonical
            score = other._alias_scores.get(alias_key)
            if score is not None:
                self._alias_scores[alias_key] = score
        for name, entity_type in other._types.items():
            self._types.setdefault(name, entity_type)  # keep an existing local tag on collision

    # -- editing ------------------------------------------------------------
    def _rebuild(self) -> None:
        """Regenerate the derived indexes (``_canon_keys``/``_canon_set``/``_blocks``).

        Structural edits (rename/delete/merge) shift canonical positions, so the append-only block
        index must be rebuilt from ``_canonicals`` afterwards.
        """
        self._canon_keys = []
        self._canon_set = set()
        self._block_index = CappedBlockIndex(cap=_BLOCK_CAP)
        canonicals = self._canonicals
        self._canonicals = []
        for name in canonicals:
            self._add_canonical(name)

    def add_canonical(self, name: str) -> None:
        """Add a canonical name (no-op if blank or already present)."""
        self._add_canonical(name)

    def rename_canonical(self, old: str, new: str) -> None:
        """Rename a canonical, repointing every alias that mapped to ``old``."""
        if old not in self._canon_set or not new or old == new:
            return
        self._canonicals = [new if c == old else c for c in self._canonicals]
        self._aliases = {k: (new if v == old else v) for k, v in self._aliases.items()}
        if old in self._types:  # carry the tag onto the new name
            self._types[new] = self._types.pop(old)
        self._rebuild()

    def delete_canonical(self, name: str) -> None:
        """Remove a canonical and every alias that pointed to it."""
        if name not in self._canon_set:
            return
        self._canonicals = [c for c in self._canonicals if c != name]
        self._aliases = {k: v for k, v in self._aliases.items() if v != name}
        self._alias_scores = {k: s for k, s in self._alias_scores.items() if k in self._aliases}
        self._types.pop(name, None)
        self._rebuild()

    def merge_canonicals(self, source: str, target: str) -> None:
        """Fold ``source`` into ``target``: repoint aliases to ``target`` and drop ``source``."""
        if source not in self._canon_set or source == target:
            return
        self._add_canonical(target)
        self._canonicals = [c for c in self._canonicals if c != source]
        self._aliases = {k: (target if v == source else v) for k, v in self._aliases.items()}
        self._aliases[clean(source)] = target  # the old canonical key now resolves to the target
        source_type = self._types.pop(source, None)
        if source_type is not None:
            self._types.setdefault(target, source_type)  # tag the target if it had none
        self._rebuild()

    def set_alias(self, alias_key: str, canonical: str) -> None:
        """Point ``alias_key`` at ``canonical`` (added if new) — a curated edit, so score 100."""
        key = clean(alias_key)
        if not key or not canonical:
            return
        self._add_canonical(canonical)
        self._aliases[key] = canonical
        self._alias_scores.pop(key, None)  # human-confirmed: no longer a marginal fuzzy learn

    def delete_alias(self, alias_key: str) -> None:
        """Remove a single alias mapping (the canonical is left in place)."""
        key = clean(alias_key)
        self._aliases.pop(key, None)
        self._alias_scores.pop(key, None)

    # -- persistence --------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        # v2: alias values are [canonical, score]; a plain string (v1) means score 100.
        # v3: adds an optional "types" map (canonical name -> type); omitted keys are untagged.
        aliases: dict[str, Any] = {}
        for key, canonical in self._aliases.items():
            score = self._alias_scores.get(key)
            aliases[key] = canonical if score is None else [canonical, score]
        return {
            "version": 3,
            "canonicals": self._canonicals,
            "aliases": aliases,
            "types": dict(self._types),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EntityMemory:
        canonicals = [str(c) for c in data.get("canonicals", [])]
        aliases: dict[str, str] = {}
        scores: dict[str, int] = {}
        raw_aliases = cast("dict[str, Any]", data.get("aliases", {}))
        for raw_key, value in raw_aliases.items():
            key = str(raw_key)
            if isinstance(value, (list, tuple)):  # v2: [canonical, score]
                pair = list(cast("Iterable[Any]", value))
                if len(pair) >= 2:
                    aliases[key] = str(pair[0])
                    score = int(pair[1])
                    if score < 100:
                        scores[key] = score
            else:  # v1: bare canonical string (treated as curated: score 100)
                aliases[key] = str(value)
        raw_types = cast("dict[str, Any]", data.get("types", {}))  # absent in v1/v2 files
        types = {str(k): str(v) for k, v in raw_types.items() if v}
        memory = cls(canonicals=canonicals, aliases=aliases, types=types)
        memory._alias_scores = scores
        return memory

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> EntityMemory:
        if not path.is_file():
            return cls()
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def seed_from_file(self, path: Path) -> int:
        """Seed canonicals/aliases from a JSON, CSV, or one-name-per-line file.

        JSON: ``{"canonicals": [...], "aliases": {...}}`` or a bare list of canonicals.
        CSV: two columns are ``alias,canonical``; one column is a canonical name.
        Other text: one canonical name per line.

        Returns the number of canonicals after seeding.
        """
        suffix = path.suffix.lower()
        if suffix == ".json":
            data: Any = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.merge(EntityMemory.from_dict(cast("dict[str, Any]", data)))
            else:
                for canonical in data:
                    self._add_canonical(str(canonical))
        elif suffix == ".csv":
            with path.open(newline="", encoding="utf-8") as handle:
                for row in csv.reader(handle):
                    if len(row) >= 2 and row[0].strip() and row[1].strip():
                        self._add_canonical(row[1].strip())
                        self._aliases[clean(row[0])] = row[1].strip()
                    elif row and row[0].strip():
                        self._add_canonical(row[0].strip())
        else:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._add_canonical(line.strip())
        return len(self._canonicals)


def review_flags(scores: list[int | None], review_threshold: int) -> list[str | None]:
    """Review flags for confidence scores: "true" for fuzzy accepts below the bar.

    0 (unmatched) and 100 (exact/identity) never flag, whatever the threshold.
    """
    cap = min(review_threshold, 100)
    return [None if s is None else ("true" if 0 < s < cap else "false") for s in scores]


def normalize_column(  # noqa: PLR0913 - a clear public entry point with keyword-only options
    table: pa.Table,
    column: str,
    target: str,
    memory: EntityMemory,
    *,
    threshold: int = DEFAULT_THRESHOLD,
    separator: str = "",
    learn: bool = True,
    scorer: str = DEFAULT_SCORER,
    score_target: str = "",
    review_target: str = "",
    review_threshold: int = 0,
    type_target: str = "",
    on_progress: OnProgress | None = None,
) -> pa.Table:
    """Return ``table`` with a ``target`` column of canonical values for ``column``.

    Fuzzy resolution runs once per **distinct** value (dictionary-encoded), then maps back over all
    rows with a single vectorized take — so cost scales with unique names, not row count. When
    ``separator`` is set, each value is split, resolved part-by-part, and rejoined (for concatenated
    multi-party columns like ``assignor_names``). ``learn=False`` matches without adding new
    canonicals; ``scorer`` selects the rapidfuzz algorithm. Calls ``on_progress(done, total)`` as
    distinct names are resolved.

    Confidence outputs (both optional): ``score_target`` adds an int column with the value's
    weakest part-confidence (100 = exact/identity, 0 = a part had no match); ``review_target``
    adds a "true"/"false" column flagging values accepted via a fuzzy match scoring below
    ``review_threshold`` — the clerical-review band. ``review_threshold=0`` disables flagging.

    ``type_target`` (optional) adds a string column with the resolved canonical's stored **entity
    type** (from :meth:`EntityMemory.entity_type`) — reusing tags applied once in the entity editor.
    Untagged canonicals yield ``""``; multi-party values report the single agreed type across their
    parts, else ``"unknown"``. Requires no re-classification at run time.
    """

    def combine_types(canonicals: list[str]) -> str:
        tags = {t for t in (memory.entity_type(c) for c in canonicals) if t}
        if not tags:
            return ""  # no part is tagged
        return next(iter(tags)) if len(tags) == 1 else "unknown"  # agreement, else ambiguous

    def resolve_value(value: str) -> tuple[str, int, str]:
        parts = [p.strip() for p in value.split(separator) if p.strip()] if separator else [value]
        canonicals: list[str] = []
        min_score = 100
        for part in parts:
            canonical, _is_new, score = memory.resolve(
                part, threshold=threshold, learn=learn, scorer=scorer
            )
            canonicals.append(canonical)
            min_score = min(min_score, score)
        joined = separator.join(canonicals) if separator else canonicals[0]
        return joined, min_score, (combine_types(canonicals) if type_target else "")

    # Route the column through Any (pyarrow-stubs under-type dictionary_encode / take).
    source: Any = table.column(column).combine_chunks()
    encoded: Any = source.dictionary_encode()
    distinct: list[Any] = encoded.dictionary.to_pylist()
    total = len(distinct)
    mapped: list[str | None] = []
    scores: list[int | None] = []
    kinds: list[str | None] = []
    for index, value in enumerate(distinct):
        if value is None:
            mapped.append(None)
            scores.append(None)
            kinds.append(None)
        else:
            canonical, score, kind = resolve_value(value)
            mapped.append(canonical)
            scores.append(score)
            kinds.append(kind)
        if on_progress is not None and (index + 1) % _PROGRESS_EVERY == 0:
            on_progress(index + 1, total)
    if on_progress is not None:
        on_progress(total, total)

    def put(result: pa.Table, name: str, values: Any) -> pa.Table:
        if name in result.column_names:
            result = result.drop_columns([name])
        return result.append_column(name, pc.take(values, encoded.indices))

    result = put(table, target, pa.array(mapped, type=pa.string()))
    if score_target:
        result = put(result, score_target, pa.array(scores, type=pa.int32()))
    if review_target:
        flags = review_flags(scores, review_threshold)
        result = put(result, review_target, pa.array(flags, type=pa.string()))
    if type_target:
        result = put(result, type_target, pa.array(kinds, type=pa.string()))
    return result
