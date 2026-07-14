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
from typing import Any, Protocol, cast

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


def _make_urllib_transport(timeout: int) -> Transport:
    """A POST transport via the stdlib (no third-party HTTP client), with a per-request timeout.

    The timeout matters for a live API: without it a stalled socket hangs the whole run.
    """

    def transport(url: str, body: bytes, headers: dict[str, str]) -> bytes:
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data: bytes = response.read()
            return data

    return transport


# Backward-compatible module-level default (30s); the API source builds a config-timed one instead.
_urllib_transport = _make_urllib_transport(30)


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


class ApiAuthError(RuntimeError):
    """Raised on an HTTP 4xx from the API (bad/missing key, bad request) — not retried."""


@dataclass(slots=True)
class UsptoOdpApiSource:
    """CPC from the USPTO ODP Patent Search API, authed via ``X-API-KEY``.

    Queries ``applicationMetaData.patentNumber`` (OR-joined per batch) at the ODP search endpoint
    and reads ``applicationMetaData.cpcClassificationBag`` from each hit. The key is read from the
    environment variable named by ``api_key_env`` — never stored on disk. Requests are batched,
    rate-limited to stay under the ODP throttle, retried with backoff on transient network errors
    (but not on 4xx), and hard-capped at ``max_api_calls`` so a large accidental fetch fails loudly.
    """

    config: CpcSourceConfig
    transport: Transport | None = None  # None ⇒ a config-timed urllib POST (tests inject a fake)

    def _api_key(self) -> str:
        key = os.environ.get(self.config.api_key_env, "")
        if not key:
            raise OfflineError(
                f"no API key: set the {self.config.api_key_env} environment variable "
                f"(the config stores only the variable name, never the key)"
            )
        return key

    def _transport(self) -> Transport:
        return self.transport or _make_urllib_transport(self.config.timeout_seconds)

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
        transport = self._transport()
        min_interval = 60.0 / cfg.rate_limit_per_min if cfg.rate_limit_per_min > 0 else 0.0
        result: dict[str, list[str]] = {}
        last_call = 0.0
        for batch in batches:
            elapsed = time.monotonic() - last_call
            if min_interval and elapsed < min_interval:
                time.sleep(min_interval - elapsed)
            result.update(self._request(batch, key, transport))
            last_call = time.monotonic()
        return result

    def _request(self, batch: list[str], key: str, transport: Transport) -> dict[str, list[str]]:
        cfg = self.config
        or_terms = " OR ".join(batch)
        body = json.dumps(
            {
                "q": f"{cfg.patent_query_field}:({or_terms})",
                "fields": [cfg.patent_query_field, cfg.cpc_response_field],
                "pagination": {"offset": 0, "limit": len(batch)},
            }
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-API-KEY": key,
        }
        raw = self._send_with_retries(transport, body, headers)
        return _parse_odp_response(raw, cfg.patent_query_field, cfg.cpc_response_field)

    def _send_with_retries(
        self, transport: Transport, body: bytes, headers: dict[str, str]
    ) -> bytes:
        cfg = self.config
        last_error: Exception | None = None
        for attempt in range(cfg.retries + 1):
            try:
                return transport(cfg.endpoint, body, headers)
            except urllib.error.HTTPError as exc:
                if 400 <= exc.code < 500:  # bad key / bad request — retrying won't help
                    detail = _http_error_detail(exc)
                    raise ApiAuthError(
                        f"USPTO ODP returned HTTP {exc.code} ({exc.reason}){detail} — "
                        f"check the API key in ${cfg.api_key_env} and the endpoint"
                    ) from exc
                last_error = exc  # 5xx: transient, retry
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
            if attempt < cfg.retries:
                time.sleep(cfg.backoff_seconds * (2**attempt))
        raise RuntimeError(
            f"USPTO ODP request failed after {cfg.retries + 1} attempts: {last_error}"
        )


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    """A short, safe snippet of an HTTP error body (never echoes the request/key)."""
    try:
        text = exc.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""
    return f": {text[:160]}" if text else ""


def _dig(obj: Any, dotted: str) -> Any:
    """Walk a dotted path (``a.b.c``) through nested dicts; ``None`` if any hop is missing."""
    current: Any = obj
    for key in dotted.split("."):
        if not isinstance(current, dict):
            return None
        current = cast("dict[str, Any]", current).get(key)
    return current


def _parse_odp_response(raw: bytes, patent_field: str, cpc_field: str) -> dict[str, list[str]]:
    """Extract ``{patent_number: [cpc symbols]}`` from an ODP Patent Search JSON response.

    Reads ``patentFileWrapperDataBag[]``; ``patent_field``/``cpc_field`` are dotted paths within
    each wrapper record (defaults live under ``applicationMetaData``). Symbols keep their raw form
    (space-padded); downstream ``grain_of`` strips whitespace.
    """
    doc: Any = json.loads(raw.decode("utf-8"))
    records: Any = doc.get("patentFileWrapperDataBag") or []
    out: dict[str, list[str]] = {}
    for record in records:
        pid: Any = _dig(record, patent_field)
        if not pid:
            continue
        codes_raw: Any = _dig(record, cpc_field) or []
        codes = [str(c) for c in codes_raw if c] if isinstance(codes_raw, list) else []  # type: ignore[misc]
        out[str(pid)] = list(dict.fromkeys(codes))  # distinct, order-preserving
    return out


def make_source(config: CpcConfig, *, transport: Transport | None = None) -> CpcSource:
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
        network_ok = allow_network and not self._config.source.offline_only
        source_allowed = (not is_network) or network_ok
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
