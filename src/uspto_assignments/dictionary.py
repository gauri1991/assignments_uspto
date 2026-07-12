"""The standalone entity-resolution dictionary: a build-once local artifact, read at run time.

Layout (under a git-ignored directory, default ``dictionary/``):

- ``entities.parquet``  — ``entity_id, canonical_name, entity_type, country, sector, source,
  ultimate_parent_id``
- ``aliases.parquet``   — ``alias_key (cleaned), entity_id, alias_source``
- ``provisional.parquet`` — off-gazetteer entities minted by previous runs (stable ids)
- ``manifest.json``     — source files, sha256 hashes, row counts, build timestamp

``build_dictionary`` is the only writer of the seed data (explicit, build-time); the pipeline only
reads. Seed sources are **optional local files** — v1 ships with the PatentsView disambiguated
assignee file; GLEIF/SEC/Wikidata drop in as additional generic sources. No network calls anywhere.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as _pq

from .normalize import clean
from .reference import iter_reference_batches

# pyarrow.parquet is under-typed in pyarrow-stubs; route through Any (see filters.py for rationale).
pq: Any = _pq

ENTITIES_FILE = "entities.parquet"
ALIASES_FILE = "aliases.parquet"
PROVISIONAL_FILE = "provisional.parquet"
MANIFEST_FILE = "manifest.json"

_ENTITY_FIELDS = [
    "entity_id",
    "canonical_name",
    "entity_type",
    "country",
    "sector",
    "source",
    "ultimate_parent_id",
]
_ALIAS_FIELDS = ["alias_key", "entity_id", "alias_source"]

# Legal-form tokens stripped (and recorded) to form a secondary exact-match key, so
# "ACME CORP" and "ACME CORPORATION INC" share the suffix-stripped key "ACME CORP"/"ACME".
_LEGAL_SUFFIXES = (
    "INCORPORATED|INC|CORPORATION|CORP|COMPANY|CO|LLC|LLP|LP|LTD|LIMITED|PLC|GMBH|AG|KG|SA|SARL|"
    "SAS|NV|BV|OY|AB|AS|SPA|SRL|KK|PTY|PTE|ULC|APS|OYJ|ASA"
)
_SUFFIX_RE = re.compile(rf"(?:\s+(?:{_LEGAL_SUFFIXES}))+$")


def strip_legal_suffix(key: str) -> str:
    """Remove trailing legal-form tokens from a cleaned key (``ACME CORP INC`` → ``ACME``)."""
    return _SUFFIX_RE.sub("", key).strip()


def provisional_id(representative_key: str) -> str:
    """A stable provisional entity id derived from a cluster representative's cleaned key."""
    return "prov-" + hashlib.sha1(representative_key.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class ResolutionDictionary:
    """The loaded artifact: O(1) alias lookup plus entity attributes for resolved ids."""

    alias_map: dict[str, str]  # cleaned alias key -> entity_id (includes provisionals)
    entities: dict[str, dict[str, str]]  # entity_id -> attribute dict (_ENTITY_FIELDS)
    canonical_keys: list[str] = field(default_factory=list[str])  # cleaned keys for fuzzy residual
    key_to_entity: dict[str, str] = field(default_factory=dict[str, str])

    def lookup_exact(self, key: str) -> str | None:
        """Exact alias hit for a cleaned key — tries the key, then its suffix-stripped form."""
        hit = self.alias_map.get(key)
        if hit is not None:
            return hit
        return self.alias_map.get(strip_legal_suffix(key))

    def entity(self, entity_id: str) -> dict[str, str]:
        return self.entities.get(entity_id, {})

    def size(self) -> tuple[int, int]:
        return len(self.entities), len(self.alias_map)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _entity_rows_from_source(
    path: Path, name_column: str, id_column: str, source: str, delimiter: str = ""
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Stream one seed source into entity + alias rows (distinct non-empty names)."""
    entities: list[dict[str, str]] = []
    aliases: list[dict[str, str]] = []
    seen: set[str] = set()
    columns = [name_column] + ([id_column] if id_column else [])
    for batch in iter_reference_batches(path, columns, delimiter):
        names = batch.column(name_column).to_pylist()
        ids = batch.column(id_column).to_pylist() if id_column else [None] * len(names)
        for raw_name, raw_id in zip(names, ids, strict=True):
            name = (raw_name or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            key = clean(name)
            if not key:
                continue
            entity_id = (
                str(raw_id) if raw_id else f"{source}-{hashlib.sha1(key.encode()).hexdigest()[:16]}"
            )
            entities.append(
                {
                    "entity_id": entity_id,
                    "canonical_name": name,
                    "entity_type": "company",
                    "country": "",
                    "sector": "",
                    "source": source,
                    "ultimate_parent_id": entity_id,  # self until a relationship source is loaded
                }
            )
            aliases.append({"alias_key": key, "entity_id": entity_id, "alias_source": source})
            stripped = strip_legal_suffix(key)
            if stripped and stripped != key:
                aliases.append(
                    {
                        "alias_key": stripped,
                        "entity_id": entity_id,
                        "alias_source": f"{source}-stripped",
                    }
                )
    return entities, aliases


def build_dictionary(
    out_dir: Path,
    *,
    patentsview: Path | None = None,
    patentsview_name_column: str = "disambig_assignee_organization",
    patentsview_id_column: str = "assignee_id",
    extra_sources: list[tuple[Path, str, str, str]] | None = None,
) -> dict[str, Any]:
    """Build the artifact from local seed files; returns the manifest (also written to disk).

    ``extra_sources`` entries are ``(path, name_column, id_column, source_name)`` — the generic
    adapter for GLEIF/SEC/Wikidata extracts or any curated name list.
    """
    sources: list[tuple[Path, str, str, str]] = []
    if patentsview is not None:
        sources.append((patentsview, patentsview_name_column, patentsview_id_column, "patentsview"))
    sources.extend(extra_sources or [])
    if not sources:
        raise ValueError("build_dictionary needs at least one seed source")

    all_entities: list[dict[str, str]] = []
    all_aliases: list[dict[str, str]] = []
    alias_seen: set[str] = set()
    manifest_sources: list[dict[str, Any]] = []
    for path, name_col, id_col, source in sources:
        entities, aliases = _entity_rows_from_source(path, name_col, id_col, source)
        all_entities.extend(entities)
        for alias in aliases:  # first source wins on alias collisions (source precedence = order)
            if alias["alias_key"] not in alias_seen:
                alias_seen.add(alias["alias_key"])
                all_aliases.append(alias)
        manifest_sources.append(
            {
                "source": source,
                "path": str(path),
                "sha256": _sha256(path),
                "entities": len(entities),
            }
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    entities_table = pa.table({name: [e[name] for e in all_entities] for name in _ENTITY_FIELDS})
    aliases_table = pa.table({name: [a[name] for a in all_aliases] for name in _ALIAS_FIELDS})
    pq.write_table(entities_table, out_dir / ENTITIES_FILE)
    pq.write_table(aliases_table, out_dir / ALIASES_FILE)
    if not (out_dir / PROVISIONAL_FILE).is_file():  # never clobber previously minted ids
        empty = pa.table(
            {
                "alias_key": pa.array([], pa.string()),
                "entity_id": pa.array([], pa.string()),
                "canonical_name": pa.array([], pa.string()),
                "entity_type": pa.array([], pa.string()),
            }
        )
        pq.write_table(empty, out_dir / PROVISIONAL_FILE)
    manifest: dict[str, Any] = {
        "built": datetime.now(UTC).isoformat(timespec="seconds"),
        "sources": manifest_sources,
        "entities": len(all_entities),
        "aliases": len(all_aliases),
    }
    (out_dir / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def load_dictionary(dict_dir: Path) -> ResolutionDictionary:
    """Load the artifact into O(1) lookup structures (entities + aliases + provisionals)."""
    entities_table = pq.read_table(dict_dir / ENTITIES_FILE)
    aliases_table = pq.read_table(dict_dir / ALIASES_FILE)
    entities: dict[str, dict[str, str]] = {}
    for row in entities_table.to_pylist():
        entities[row["entity_id"]] = {k: (row.get(k) or "") for k in _ENTITY_FIELDS}
    alias_map: dict[str, str] = {}
    for row in aliases_table.to_pylist():
        alias_map.setdefault(row["alias_key"], row["entity_id"])
    canonical_keys: list[str] = []
    key_to_entity: dict[str, str] = {}
    for entity_id, attrs in entities.items():
        key = clean(attrs["canonical_name"])
        if key and key not in key_to_entity:
            canonical_keys.append(key)
            key_to_entity[key] = entity_id
    provisional_path = dict_dir / PROVISIONAL_FILE
    if provisional_path.is_file():
        for row in pq.read_table(provisional_path).to_pylist():
            alias_map.setdefault(row["alias_key"], row["entity_id"])
            entities.setdefault(
                row["entity_id"],
                {
                    "entity_id": row["entity_id"],
                    "canonical_name": row["canonical_name"],
                    "entity_type": row.get("entity_type") or "company",
                    "country": "",
                    "sector": "",
                    "source": "provisional",
                    "ultimate_parent_id": row["entity_id"],
                },
            )
    return ResolutionDictionary(
        alias_map=alias_map,
        entities=entities,
        canonical_keys=canonical_keys,
        key_to_entity=key_to_entity,
    )


def append_provisionals(dict_dir: Path, rows: list[dict[str, str]]) -> int:
    """Persist newly minted provisional entities (id-stable across runs); returns total rows."""
    path = dict_dir / PROVISIONAL_FILE
    existing: list[dict[str, str]] = (
        cast("list[dict[str, str]]", pq.read_table(path).to_pylist()) if path.is_file() else []
    )
    known = {r["alias_key"] for r in existing}
    merged = existing + [r for r in rows if r["alias_key"] not in known]
    table = pa.table(
        {
            "alias_key": [r["alias_key"] for r in merged],
            "entity_id": [r["entity_id"] for r in merged],
            "canonical_name": [r["canonical_name"] for r in merged],
            "entity_type": [r.get("entity_type", "company") for r in merged],
        }
    )
    dict_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    return len(merged)
