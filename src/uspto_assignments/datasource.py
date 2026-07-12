"""Pluggable CPC data sources + a cache-first, offline-by-default fetch layer.

A :class:`CpcSource` maps normalized **grant** patent ids to their CPC symbols. Two implementations
ship: :class:`LocalFileCpcSource` (a bulk TSV/CSV/Parquet table — offline, scale-safe) and
:class:`UsptoOdpApiSource` (the USPTO Open Data Portal / PatentSearch API — for incremental sets).
Adding a new provider or enrichment field is one new :class:`CpcSource` implementation plus config
entries; the matching pipeline never changes.

:class:`CpcCache` wraps any source: it persists fetched CPC to a compact Parquet keyed by normalized
patent id (with a TTL), serves cache hits offline, and only calls the source for cache misses — and
only when the network is explicitly enabled for the run. This is what keeps a ``match`` run offline
and reproducible after the first fetch, while ``offline_only`` hard-disables the network entirely.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import duckdb as _duckdb
import pyarrow as pa
import pyarrow.parquet as _pq

from .cpcconfig import CpcConfig, CpcSourceConfig

duckdb: Any = _duckdb  # duckdb is under-typed; route through Any (matches ledger.py).
pq: Any = _pq  # pyarrow.parquet is under-typed in the stubs; route through Any (see filters.py).

logger = logging.getLogger(__name__)

CACHE_FILENAME = "cpc_cache.parquet"

# A transport is (url, body, headers) -> response bytes. Injectable so tests never hit the network.
Transport = Callable[[str, bytes, dict[str, str]], bytes]


class OfflineError(RuntimeError):
    """Raised when a fetch would require the network but it is disabled for this run."""


class ApiBudgetError(RuntimeError):
    """Raised when a fetch would exceed the configured ``max_api_calls`` guard."""


class CpcSource(Protocol):
    """Maps normalized grant ids to their CPC symbols. Unresolved ids are absent from the result."""

    def fetch(self, grant_ids: list[str]) -> dict[str, list[str]]:
        """Return ``{grant_id: [full CPC symbols]}`` for the ids that resolve (others omitted)."""
        ...


def _urllib_transport(url: str, body: bytes, headers: dict[str, str]) -> bytes:
    """Default transport: a single POST via the standard library (no third-party HTTP client)."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request) as response:
        data: bytes = response.read()
        return data


@dataclass(slots=True)
class LocalFileCpcSource:
    """CPC from a bulk local file (TSV/CSV/Parquet), keyed by a bare grant number. Fully offline."""

    path: Path
    patent_column: str = "patent_id"
    code_column: str = "cpc_group"

    def fetch(self, grant_ids: list[str]) -> dict[str, list[str]]:
        """Look up ``grant_ids`` in the bulk file via DuckDB, returning distinct CPC symbols."""
        if not grant_ids:
            return {}
        if not self.path.is_file():
            raise FileNotFoundError(f"CPC source file not found: {self.path}")
        reader = "read_parquet" if self.path.suffix.lower() == ".parquet" else "read_csv_auto"
        wanted = pa.table({"pid": pa.array(grant_ids, type=pa.string())})
        con = duckdb.connect()
        try:
            con.register("wanted", wanted)
            rows = con.execute(
                f"SELECT CAST(s.{self.patent_column} AS VARCHAR) AS pid, "
                f"       list(DISTINCT CAST(s.{self.code_column} AS VARCHAR)) AS codes "
                f"FROM {reader}('{self.path.as_posix()}') s "
                f"JOIN wanted w ON CAST(s.{self.patent_column} AS VARCHAR) = w.pid "
                f"WHERE s.{self.code_column} IS NOT NULL AND s.{self.code_column} <> '' "
                f"GROUP BY 1"
            ).fetchall()
        finally:
            con.close()
        return {str(pid): [str(c) for c in codes] for pid, codes in rows}


@dataclass(slots=True)
class UsptoOdpApiSource:
    """CPC from the USPTO ODP / PatentSearch API (POST by ``patent_id``), authed via ``X-Api-Key``.

    The key is read from the environment variable named by ``api_key_env`` — never stored on disk.
    Requests are batched, rate-limited to stay under the ODP throttle, retried with backoff, and
    hard-capped at ``max_api_calls`` so a large accidental fetch fails loudly instead of firing
    thousands of calls.
    """

    config: CpcSourceConfig
    transport: Transport = _urllib_transport

    def _api_key(self) -> str:
        key = os.environ.get(self.config.api_key_env, "")
        if not key:
            raise OfflineError(
                f"no API key: set the {self.config.api_key_env} environment variable "
                f"(the config stores only the variable name, never the key)"
            )
        return key

    def fetch(self, grant_ids: list[str]) -> dict[str, list[str]]:
        if not grant_ids:
            return {}
        cfg = self.config
        batches = [
            grant_ids[i : i + cfg.batch_size] for i in range(0, len(grant_ids), cfg.batch_size)
        ]
        if len(batches) > cfg.max_api_calls:
            raise ApiBudgetError(
                f"fetch needs {len(batches)} API requests but max_api_calls={cfg.max_api_calls}; "
                f"raise the cap, narrow the input, or use a local_file source for large sets"
            )
        key = self._api_key()
        list_field, _, item_field = cfg.code_field.partition(".")
        min_interval = 60.0 / cfg.rate_limit_per_min if cfg.rate_limit_per_min > 0 else 0.0
        result: dict[str, list[str]] = {}
        last_call = 0.0
        for batch in batches:
            elapsed = time.monotonic() - last_call
            if min_interval and elapsed < min_interval:
                time.sleep(min_interval - elapsed)
            payload = self._request(batch, key, list_field, item_field)
            last_call = time.monotonic()
            result.update(payload)
        return result

    def _request(
        self, batch: list[str], key: str, list_field: str, item_field: str
    ) -> dict[str, list[str]]:
        cfg = self.config
        body = json.dumps(
            {
                "q": {"patent_id": batch},
                "f": ["patent_id", cfg.code_field],
                "o": {"size": len(batch)},
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "X-Api-Key": key}
        raw = self._send_with_retries(body, headers)
        return _parse_odp_response(raw, list_field, item_field)

    def _send_with_retries(self, body: bytes, headers: dict[str, str]) -> bytes:
        cfg = self.config
        last_error: Exception | None = None
        for attempt in range(cfg.retries + 1):
            try:
                return self.transport(cfg.endpoint, body, headers)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt < cfg.retries:
                    time.sleep(cfg.backoff_seconds * (2**attempt))
        raise RuntimeError(
            f"USPTO ODP request failed after {cfg.retries + 1} attempts: {last_error}"
        )


def _parse_odp_response(raw: bytes, list_field: str, item_field: str) -> dict[str, list[str]]:
    """Extract ``{patent_id: [cpc symbols]}`` from an ODP/PatentSearch JSON response body."""
    doc: Any = json.loads(raw.decode("utf-8"))
    patents: Any = doc.get("patents") or []
    out: dict[str, list[str]] = {}
    for patent in patents:
        pid: Any = patent.get("patent_id")
        if not pid:
            continue
        codes: list[str] = []
        entries: Any = patent.get(list_field) or []
        for entry in entries:
            symbol: Any = entry.get(item_field) if hasattr(entry, "get") else None
            if symbol:
                codes.append(str(symbol))
        # keep distinct while preserving order
        out[str(pid)] = list(dict.fromkeys(codes))
    return out


def make_source(config: CpcConfig, *, transport: Transport = _urllib_transport) -> CpcSource:
    """Build the configured :class:`CpcSource` (``transport`` is injectable for tests)."""
    src = config.source
    if src.type == "local_file":
        return LocalFileCpcSource(
            path=Path(src.path), patent_column=src.patent_column, code_column=src.code_column
        )
    return UsptoOdpApiSource(config=src, transport=transport)


@dataclass(slots=True)
class FetchResult:
    """Outcome of resolving a set of grant ids against the cache (+ optional network fetch)."""

    codes: dict[str, list[str]]  # one entry per LOOKED-UP id (value may be an empty list)
    uncached_offline: list[str]  # requested ids with no cache entry and no network permitted
    fetched: int  # how many ids were freshly fetched from the source this call

    def found(self) -> dict[str, list[str]]:
        """Only the ids that resolved to at least one CPC symbol."""
        return {pid: codes for pid, codes in self.codes.items() if codes}


class CpcCache:
    """Cache-first resolver: serves CPC from a local Parquet, fetching misses only when allowed."""

    def __init__(self, config: CpcConfig, source: CpcSource, *, now: float | None = None) -> None:
        self._config = config
        self._source = source
        self._now = now if now is not None else time.time()
        self._dir = Path(config.cache.path)
        self._file = self._dir / CACHE_FILENAME
        self._entries: dict[str, tuple[list[str], float]] = self._load()

    def _load(self) -> dict[str, tuple[list[str], float]]:
        if not self._file.is_file():
            return {}
        table = pq.read_table(self._file)
        ids: list[Any] = table.column("patent_id").to_pylist()
        code_lists: list[Any] = table.column("cpc_codes").to_pylist()
        stamps: list[Any] = table.column("fetched_at").to_pylist()
        entries: dict[str, tuple[list[str], float]] = {}
        for pid, code_list, ts in zip(ids, code_lists, stamps, strict=True):
            codes: list[str] = [str(c) for c in code_list] if code_list else []
            entries[str(pid)] = (codes, float(ts))
        return entries

    def _is_fresh(self, fetched_at: float) -> bool:
        ttl_seconds = self._config.cache.ttl_days * 86400
        return ttl_seconds <= 0 or (self._now - fetched_at) < ttl_seconds

    def resolve(self, grant_ids: list[str], *, allow_network: bool) -> FetchResult:
        """Resolve ``grant_ids`` to CPC codes: cache first, then fetch misses if the network is on.

        ``allow_network`` gates the network for this call; it is additionally AND-ed with the
        source config's ``offline_only`` (which, when True, blocks the network unconditionally).
        """
        unique = list(dict.fromkeys(pid for pid in grant_ids if pid))
        codes: dict[str, list[str]] = {}
        misses: list[str] = []
        for pid in unique:
            entry = self._entries.get(pid)
            if entry is not None and self._is_fresh(entry[1]):
                codes[pid] = entry[0]
            else:
                misses.append(pid)

        # A local_file source is offline — always allowed. A network source (the API) needs the
        # per-run opt-in AND config not set to offline_only.
        is_network = self._config.source.type != "local_file"
        source_allowed = (not is_network) or (allow_network and not self._config.source.offline_only)
        fetched = 0
        if misses and source_allowed:
            found = self._source.fetch(misses)
            fetched = len(misses)
            for (
                pid
            ) in misses:  # record every looked-up id (empty list = fetched, no CPC) with a stamp
                pid_codes = found.get(pid, [])
                codes[pid] = pid_codes
                self._entries[pid] = (pid_codes, self._now)
            self._save()
            uncached_offline: list[str] = []
        else:
            uncached_offline = misses
        return FetchResult(codes=codes, uncached_offline=uncached_offline, fetched=fetched)

    def _save(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        ids = list(self._entries)
        table = pa.table(
            {
                "patent_id": pa.array(ids, type=pa.string()),
                "cpc_codes": pa.array(
                    [self._entries[pid][0] for pid in ids], type=pa.list_(pa.string())
                ),
                "fetched_at": pa.array([self._entries[pid][1] for pid in ids], type=pa.float64()),
            }
        )
        pq.write_table(table, self._file)


@dataclass(slots=True)
class CpcRunContext:
    """Per-run CPC settings threaded into the batch engine for the CPC steps.

    ``allow_network`` is the explicit per-run opt-in (e.g. the UI "Allow network" checkbox); it is
    only honored when the config's ``source.offline_only`` is also False.
    """

    config: CpcConfig = field(default_factory=CpcConfig)
    allow_network: bool = False
