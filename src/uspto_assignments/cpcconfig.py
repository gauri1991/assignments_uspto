"""Configuration for the CPC data-fetch layer and portfolio-matching steps.

A single project-local config file (``cpc_config.json`` by default) controls where CPC codes come
from (a local bulk file, or the USPTO Open Data Portal / PatentSearch API), how the fetched data is
cached, and the defaults for the ``cpc_match`` step. It is edited in the UI (*Settings ▸ CPC data
source*) and saved in the project, mirroring the ``entities.json`` / ``batch_templates.json`` idiom.

**Security:** the API key is never stored here — the config holds only ``api_key_env`` (the name of
an environment variable), and the key is read from the environment at run time.

**Offline by default:** ``source.offline_only`` defaults to ``True`` — a ``fetch_cpc`` step reads
the cache only and reports uncached numbers; a run must explicitly enable the network to fetch. Once
the cache is populated, every run is offline and reproducible.

Endpoint/auth facts are captured with an ``as_of`` date and must be re-verified against live docs:
PatentsView's legacy API (``api.patentsview.org``) returns ``410 Gone`` since 2025-05-01, and
PatentsView migrated to the USPTO Open Data Portal (``data.uspto.gov``) on 2026-03-20. The live
surface is the ODP Patent File Wrapper Search API — ``POST`` to
``https://api.uspto.gov/api/v1/patent/applications/search``, querying
``applicationMetaData.patentNumber`` (a bare grant number, e.g. ``10987654``) and requesting
``applicationMetaData.cpcClassificationBag`` (a list of full CPC symbols), authenticated with an
``X-API-KEY`` header (throttle ~45 requests per minute).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

# The project-local config filename (relocatable; holds no secret, so it is safe to share/commit).
CPC_CONFIG_FILENAME = "cpc_config.json"

# Endpoint/auth defaults — verified live 2026-07 against https://api.uspto.gov; re-verify against
# https://data.uspto.gov live docs.
CPC_ENDPOINT_AS_OF = "2026-07"
DEFAULT_ODP_ENDPOINT = "https://api.uspto.gov/api/v1/patent/applications/search"
DEFAULT_API_KEY_ENV = "USPTO_ODP_API_KEY"
# ODP request/response field paths: the query field selecting a grant number, and the response
# field carrying its CPC symbols (both under ``applicationMetaData``).
DEFAULT_PATENT_QUERY_FIELD = "applicationMetaData.patentNumber"
DEFAULT_CPC_RESPONSE_FIELD = "applicationMetaData.cpcClassificationBag"

SourceType = Literal["local_file", "uspto_odp_api"]
CpcGrain = Literal["subclass", "main_group", "full_symbol"]
OverlapMetric = Literal["shared_count", "jaccard", "rarity_weighted"]

_SOURCE_TYPES: frozenset[str] = frozenset({"local_file", "uspto_odp_api"})
_GRAINS: frozenset[str] = frozenset({"subclass", "main_group", "full_symbol"})
_METRICS: frozenset[str] = frozenset({"shared_count", "jaccard", "rarity_weighted"})

# Hosts that no longer serve the CPC API — a saved config pointing here is silently repaired to the
# live ODP endpoint on load (PatentsView migrated to data.uspto.gov on 2026-03-20).
_DEAD_ENDPOINT_HOSTS = ("search.patentsview.org", "api.patentsview.org")


def _migrate_endpoint(endpoint: str) -> str:
    """Repair a saved config that still points at a decommissioned PatentsView host."""
    if any(host in endpoint for host in _DEAD_ENDPOINT_HOSTS):
        return DEFAULT_ODP_ENDPOINT
    return endpoint


@dataclass(slots=True)
class CpcSourceConfig:
    """Where CPC codes are fetched from, and the network guardrails for the API source."""

    type: SourceType = "uspto_odp_api"
    # local_file source: a bulk CPC table (TSV/CSV/Parquet) keyed by ``patent_column``.
    path: str = ""
    patent_column: str = "patent_id"  # local-file join key column (bare grant number)
    code_column: str = "cpc_group"  # local-file CPC-symbol column (PatentsView g_cpc_current)
    # API source: endpoint + the ODP query/response field paths; the key comes from ``api_key_env``.
    endpoint: str = DEFAULT_ODP_ENDPOINT
    api_key_env: str = DEFAULT_API_KEY_ENV
    patent_query_field: str = DEFAULT_PATENT_QUERY_FIELD  # query field selecting a grant number
    cpc_response_field: str = DEFAULT_CPC_RESPONSE_FIELD  # response field carrying the CPC symbols
    batch_size: int = 100  # patents per API request (OR-joined in the query)
    rate_limit_per_min: int = 45  # ODP throttle; the fetcher paces requests to stay under this
    max_api_calls: int = 1000  # hard guard: refuse to fire more than this many requests in one run
    retries: int = 3
    backoff_seconds: float = 1.0
    timeout_seconds: int = 30  # per-request socket timeout (a stalled call fails, not hangs)
    offline_only: bool = True  # True ⇒ never touch the network; cache-only, report misses
    as_of: str = CPC_ENDPOINT_AS_OF

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CpcSourceConfig:
        source_type = str(data.get("type", "uspto_odp_api"))
        if source_type not in _SOURCE_TYPES:
            raise ValueError(f"unknown CPC source type: {source_type!r}")
        return cls(
            type=source_type,  # type: ignore[arg-type]  # validated against _SOURCE_TYPES above
            path=str(data.get("path", "")),
            patent_column=str(data.get("patent_column", "patent_id")),
            code_column=str(data.get("code_column", "cpc_group")),
            endpoint=_migrate_endpoint(str(data.get("endpoint", DEFAULT_ODP_ENDPOINT))),
            api_key_env=str(data.get("api_key_env", DEFAULT_API_KEY_ENV)),
            patent_query_field=str(data.get("patent_query_field", DEFAULT_PATENT_QUERY_FIELD)),
            cpc_response_field=str(data.get("cpc_response_field", DEFAULT_CPC_RESPONSE_FIELD)),
            batch_size=int(data.get("batch_size", 100)),
            rate_limit_per_min=int(data.get("rate_limit_per_min", 45)),
            max_api_calls=int(data.get("max_api_calls", 1000)),
            retries=int(data.get("retries", 3)),
            backoff_seconds=float(data.get("backoff_seconds", 1.0)),
            timeout_seconds=int(data.get("timeout_seconds", 30)),
            offline_only=bool(data.get("offline_only", True)),
            as_of=str(data.get("as_of", CPC_ENDPOINT_AS_OF)),
        )


@dataclass(slots=True)
class CpcCacheConfig:
    """The local CPC cache: a compact Parquet keyed by normalized patent id, with a TTL."""

    path: str = "data/cpc"
    ttl_days: int = 30

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CpcCacheConfig:
        return cls(path=str(data.get("path", "data/cpc")), ttl_days=int(data.get("ttl_days", 30)))


@dataclass(slots=True)
class CpcMatchConfig:
    """Defaults for the ``cpc_match`` step: grain, overlap metric/threshold, and ranking weights."""

    grain: CpcGrain = "subclass"
    overlap_metric: OverlapMetric = "shared_count"
    overlap_threshold: float = 1.0  # keep buyer patents whose overlap score ≥ this
    min_in_domain_patents: int = 1  # drop buyers with fewer than this many in-domain patents
    hit_rate_floor: float = 0.5  # abort the match if the CPC join hit-rate falls below this
    weight_overlap: float = 1.0
    weight_recency: float = 1.0
    weight_volume: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CpcMatchConfig:
        grain = str(data.get("grain", "subclass"))
        metric = str(data.get("overlap_metric", "shared_count"))
        if grain not in _GRAINS:
            raise ValueError(f"unknown CPC grain: {grain!r}")
        if metric not in _METRICS:
            raise ValueError(f"unknown overlap metric: {metric!r}")
        return cls(
            grain=grain,  # type: ignore[arg-type]  # validated against _GRAINS above
            overlap_metric=metric,  # type: ignore[arg-type]  # validated against _METRICS above
            overlap_threshold=float(data.get("overlap_threshold", 1.0)),
            min_in_domain_patents=int(data.get("min_in_domain_patents", 1)),
            hit_rate_floor=float(data.get("hit_rate_floor", 0.5)),
            weight_overlap=float(data.get("weight_overlap", 1.0)),
            weight_recency=float(data.get("weight_recency", 1.0)),
            weight_volume=float(data.get("weight_volume", 1.0)),
        )


@dataclass(slots=True)
class CpcConfig:
    """The full CPC configuration: data source, cache, and match defaults."""

    source: CpcSourceConfig = field(default_factory=CpcSourceConfig)
    cache: CpcCacheConfig = field(default_factory=CpcCacheConfig)
    match: CpcMatchConfig = field(default_factory=CpcMatchConfig)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.to_dict(),
            "cache": self.cache.to_dict(),
            "match": self.match.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CpcConfig:
        return cls(
            source=CpcSourceConfig.from_dict(data.get("source", {})),
            cache=CpcCacheConfig.from_dict(data.get("cache", {})),
            match=CpcMatchConfig.from_dict(data.get("match", {})),
        )


def load_config(path: Path) -> CpcConfig:
    """Load a :class:`CpcConfig` from ``path`` (defaults if the file is missing)."""
    if not path.is_file():
        return CpcConfig()
    return CpcConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))


def save_config(config: CpcConfig, path: Path) -> None:
    """Write ``config`` to ``path`` as pretty JSON (the API key is never included)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config.to_dict(), indent=2), encoding="utf-8")
