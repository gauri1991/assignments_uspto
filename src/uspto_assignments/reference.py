"""Match raw names against an external reference gazetteer of disambiguated organization names.

USPTO/PatentsView publishes *disambiguated assignee* data (raw assignee mentions resolved to
canonical organization names + a stable ``assignee_id``). Loaded as a reference, it becomes an
authoritative **company gazetteer**: a raw assignor name that fuzzy-matches a disambiguated org is a
known company (kept and normalized to the disambiguated name); one that doesn't is a presumed
individual. This module streams such a file (multi-GB TSV, or a compact pre-built extract), builds a
blocked-fuzzy :class:`~uspto_assignments.normalize.EntityMemory`, and adds match columns to a table.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as _pc_module
import pyarrow.csv as _pa_csv
import pyarrow.parquet as _pq

from .normalize import DEFAULT_SCORER, DEFAULT_THRESHOLD, EntityMemory, review_flags

# pyarrow.compute / pyarrow.csv are under-typed in the stubs; route through Any (see filters.py).
pc: Any = _pc_module
pa_csv: Any = _pa_csv
pq: Any = _pq

logger = logging.getLogger(__name__)

OnProgress = Callable[[int, int], None]
_PROGRESS_EVERY = 500
_READ_BLOCK_SIZE = 64 << 20  # 64 MiB CSV read blocks — streams multi-GB files with bounded memory


def _delimiter_for(path: Path, override: str) -> str:
    """The column delimiter for ``path`` (explicit ``override`` wins; else by extension)."""
    if override:
        return override
    return "," if path.suffix.lower() == ".csv" else "\t"


def iter_reference_batches(path: Path, columns: list[str], delimiter: str) -> Any:
    """Yield record batches of ``columns`` from a delimited or Parquet file (streaming)."""
    if path.suffix.lower() == ".parquet":
        parquet_file: Any = pq.ParquetFile(path)
        yield from parquet_file.iter_batches(columns=columns)
        return
    reader: Any = pa_csv.open_csv(
        path,
        read_options=pa_csv.ReadOptions(block_size=_READ_BLOCK_SIZE),
        parse_options=pa_csv.ParseOptions(delimiter=_delimiter_for(path, delimiter)),
        convert_options=pa_csv.ConvertOptions(
            include_columns=columns,
            column_types={c: pa.string() for c in columns},
            strings_can_be_null=True,
        ),
    )
    try:
        while True:
            yield reader.read_next_batch()
    except StopIteration:
        return


@dataclass(slots=True)
class ReferenceGazetteer:
    """A blocked-fuzzy set of disambiguated org names, with an optional org→id map."""

    memory: EntityMemory
    ids: dict[str, str] = field(default_factory=dict[str, str])

    def match(
        self, name: str, *, threshold: int = DEFAULT_THRESHOLD, scorer: str = DEFAULT_SCORER
    ) -> tuple[str | None, str | None, int]:
        """Return ``(disambiguated_name, id, score)`` for ``name`` (Nones + 0 if not found)."""
        matched = self.memory.match(name, threshold=threshold, scorer=scorer)
        if matched is None:
            return None, None, 0
        canonical, score = matched
        return canonical, self.ids.get(canonical), score

    def size(self) -> int:
        return self.memory.counts()[0]


def build_reference(
    path: Path, name_column: str, *, id_column: str = "", delimiter: str = ""
) -> ReferenceGazetteer:
    """Stream ``path`` and build a gazetteer of distinct non-empty ``name_column`` organizations.

    ``id_column`` (optional) is captured as the org→id map. Memory stays bounded to the *distinct*
    organizations even on multi-GB input, because only distinct names are retained.
    """
    columns = [name_column] + ([id_column] if id_column else [])
    names: list[str] = []
    seen: set[str] = set()
    ids: dict[str, str] = {}
    for batch in iter_reference_batches(path, columns, delimiter):
        org_values = batch.column(name_column).to_pylist()
        id_values = batch.column(id_column).to_pylist() if id_column else [None] * len(org_values)
        for raw_org, entity_id in zip(org_values, id_values, strict=True):
            org = raw_org.strip() if raw_org else ""
            if org and org not in seen:  # individual rows have an empty org — naturally skipped
                seen.add(org)
                names.append(org)
                if entity_id:
                    ids[org] = str(entity_id)
    memory = EntityMemory(canonicals=names)
    if logger.isEnabledFor(logging.DEBUG):  # block-distribution diagnostics under -v
        logger.debug(
            "gazetteer '%s': %d distinct orgs, largest fuzzy block %d (capped/re-split)",
            name_column,
            len(names),
            memory.max_block(),
        )
    return ReferenceGazetteer(memory=memory, ids=ids)


def extract_distinct_reference(
    src: Path, dst: Path, *, name_column: str, id_column: str = "", delimiter: str = ""
) -> int:
    """Write a compact Parquet of distinct ``(organization[, assignee_id])`` from a big reference.

    A one-time pre-build so the huge TSV is scanned once; the step then reloads ``dst`` instantly.
    Returns the number of distinct organizations written.
    """
    gazetteer = build_reference(src, name_column, id_column=id_column, delimiter=delimiter)
    orgs = gazetteer.memory.canonicals
    arrays = {"organization": pa.array(orgs, type=pa.string())}
    if id_column:
        arrays["assignee_id"] = pa.array([gazetteer.ids.get(o, "") for o in orgs], type=pa.string())
    dst.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(arrays), dst)
    return len(orgs)


# In-process cache so a big reference file is parsed once per run (once per worker process).
_CACHE: dict[tuple[str, float, str, str], ReferenceGazetteer] = {}


def load_reference(
    path: Path, name_column: str, *, id_column: str = "", delimiter: str = ""
) -> ReferenceGazetteer:
    """Build (or return a cached) gazetteer for ``path``, keyed by path + mtime + columns."""
    key = (str(path), path.stat().st_mtime, name_column, id_column)
    cached = _CACHE.get(key)
    if cached is None:
        cached = build_reference(path, name_column, id_column=id_column, delimiter=delimiter)
        _CACHE[key] = cached
    return cached


def match_column(  # noqa: PLR0913, PLR0915 - one linear pass building several output columns
    table: pa.Table,
    column: str,
    gazetteer: ReferenceGazetteer,
    target: str,
    matched_col: str,
    id_col: str,
    *,
    threshold: int = DEFAULT_THRESHOLD,
    scorer: str = DEFAULT_SCORER,
    separator: str = "",
    mode: str = "any",
    score_col: str = "",
    review_col: str = "",
    review_threshold: int = 0,
    on_progress: OnProgress | None = None,
) -> pa.Table:
    """Return ``table`` with reference-match columns for ``column``.

    Adds ``target`` (raw names replaced by the matched disambiguated org, unmatched kept as-is),
    ``matched_col`` ("true"/"false"), and — when ``id_col`` is non-empty — the matched org's id.
    Multi-party values (``separator`` set) match each part; ``mode`` combines the parts' match flags
    (``"any"`` ⇒ matched if any party is a known company, ``"all"`` ⇒ every party). Matching runs
    once per **distinct** value (dictionary-encoded), then maps back over all rows.

    Confidence outputs (both optional): ``score_col`` adds an int column with the weakest score
    among the matched parts (0 when nothing matched); ``review_col`` adds "true"/"false" flagging
    values whose weakest accepted match scored below ``review_threshold`` (0 disables flagging).
    """

    def resolve_value(value: str) -> tuple[str, bool, str, int]:
        parts = [p.strip() for p in value.split(separator) if p.strip()] if separator else [value]
        disamb: list[str] = []
        flags: list[bool] = []
        first_id = ""
        min_matched_score = 100
        for part in parts:
            canonical, entity_id, score = gazetteer.match(part, threshold=threshold, scorer=scorer)
            disamb.append(canonical if canonical is not None else part)
            flags.append(canonical is not None)
            if canonical is not None:
                min_matched_score = min(min_matched_score, score)
            if entity_id and not first_id:
                first_id = entity_id
        matched = all(flags) if mode == "all" else any(flags)
        joined = separator.join(disamb) if separator else disamb[0]
        return joined, matched, first_id, (min_matched_score if any(flags) else 0)

    source: Any = table.column(column).combine_chunks()
    encoded: Any = source.dictionary_encode()
    distinct: list[Any] = encoded.dictionary.to_pylist()
    total = len(distinct)
    disamb_vals: list[str | None] = []
    matched_vals: list[str | None] = []
    id_vals: list[str | None] = []
    score_vals: list[int | None] = []
    for index, value in enumerate(distinct):
        if value is None:
            disamb_vals.append(None)
            matched_vals.append(None)
            id_vals.append(None)
            score_vals.append(None)
        else:
            joined, matched, entity_id, score = resolve_value(value)
            disamb_vals.append(joined)
            matched_vals.append("true" if matched else "false")
            id_vals.append(entity_id)
            score_vals.append(score)
        if on_progress is not None and (index + 1) % _PROGRESS_EVERY == 0:
            on_progress(index + 1, total)
    if on_progress is not None:
        on_progress(total, total)

    indices: Any = encoded.indices

    def put(result: pa.Table, name: str, values: Any) -> pa.Table:
        if name in result.column_names:
            result = result.drop_columns([name])
        return result.append_column(name, pc.take(values, indices))

    result = put(table, target, pa.array(disamb_vals, type=pa.string()))
    result = put(result, matched_col, pa.array(matched_vals, type=pa.string()))
    if id_col:
        result = put(result, id_col, pa.array(id_vals, type=pa.string()))
    if score_col:
        result = put(result, score_col, pa.array(score_vals, type=pa.int32()))
    if review_col:
        flags_out = review_flags(score_vals, review_threshold)
        result = put(result, review_col, pa.array(flags_out, type=pa.string()))
    return result


def matched_mask(table: pa.Table, matched_col: str) -> Any:
    """A boolean array: True where ``matched_col`` == "true" (for keep/drop actions)."""
    return pc.equal(table.column(matched_col), pa.scalar("true"))
