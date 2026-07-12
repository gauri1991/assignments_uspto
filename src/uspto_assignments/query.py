"""Saved queries: a named, serializable bundle of filter state for later retrieval.

A :class:`Query` captures everything needed to reproduce a view — the table it targets, the
filter clauses and how they combine (AND/OR), the quick search, and the sort. Queries round-trip
to JSON so the UI can persist and re-apply them across sessions.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .filters import CombineMode, FilterClause, SortSpec


@dataclass(slots=True)
class Query:
    """A named filter/sort configuration for one table."""

    name: str
    table: str
    combine: CombineMode = "and"
    quick_search: str | None = None
    clauses: list[FilterClause] = field(default_factory=list[FilterClause])
    sort: SortSpec | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict."""
        return {
            "name": self.name,
            "table": self.table,
            "combine": self.combine,
            "quick_search": self.quick_search,
            "clauses": [asdict(c) for c in self.clauses],
            "sort": [self.sort[0], self.sort[1]] if self.sort is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Query:
        """Rebuild a Query from :meth:`to_dict` output (tolerant of missing keys)."""
        clauses: list[FilterClause] = [
            FilterClause(
                column=str(c["column"]),
                op=c["op"],
                value=str(c.get("value", "")),
                value2=str(c.get("value2", "")),
                case_sensitive=bool(c.get("case_sensitive", False)),
            )
            for c in data.get("clauses", [])
        ]
        raw_sort = data.get("sort")
        sort: SortSpec | None = (str(raw_sort[0]), bool(raw_sort[1])) if raw_sort else None
        return cls(
            name=str(data["name"]),
            table=str(data.get("table", "")),
            combine=data.get("combine", "and"),
            quick_search=data.get("quick_search"),
            clauses=clauses,
            sort=sort,
        )


def dump_queries(queries: list[Query], path: Path) -> None:
    """Write ``queries`` to ``path`` as JSON (creating parent directories)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [q.to_dict() for q in queries]
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def load_queries(path: Path) -> list[Query]:
    """Read queries from ``path`` (returns ``[]`` if the file does not exist)."""
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Query.from_dict(item) for item in data]
