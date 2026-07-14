"""Small on-disk persistence for the UI: recent files and saved queries.

Stored as JSON under the OS app-config location (``QStandardPaths.AppConfigLocation``) so there is
no new dependency. Both stores accept an explicit path (used by tests) and otherwise default to
``<config>/uspto-assignment-viewer/``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QStandardPaths

from uspto_assignments import (
    BatchTemplate,
    CpcConfig,
    EntityMemory,
    Query,
    dump_queries,
    dump_templates,
    load_config,
    load_queries,
    load_templates,
    save_config,
)
from uspto_assignments.cpcconfig import CPC_CONFIG_FILENAME

_APP_DIR = "uspto-assignment-viewer"
_RECENT_LIMIT = 8


def config_dir() -> Path:
    """Return the writable app-config directory (falling back to ~/.config)."""
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
    root = Path(base) if base else Path.home() / ".config"
    return root / _APP_DIR


@dataclass(frozen=True, slots=True)
class RecentEntry:
    """A recently opened source: a file (``xml``/``zip``) or a dataset folder."""

    path: str
    kind: str  # "file" | "dataset"


class RecentStore:
    """A capped most-recently-used list of opened files/datasets."""

    def __init__(self, path: Path | None = None, limit: int = _RECENT_LIMIT) -> None:
        self._path = path if path is not None else config_dir() / "recent.json"
        self._limit = limit

    def load(self) -> list[RecentEntry]:
        if not self._path.is_file():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [RecentEntry(path=str(i["path"]), kind=str(i.get("kind", "file"))) for i in raw]

    def add(self, path: str, kind: str) -> None:
        """Record ``path`` at the front, de-duplicating and capping the list."""
        entries = [e for e in self.load() if e.path != path]
        entries.insert(0, RecentEntry(path=path, kind=kind))
        entries = entries[: self._limit]
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = [{"path": e.path, "kind": e.kind} for e in entries]
        self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def clear(self) -> None:
        self._path.unlink(missing_ok=True)


class QueryStore:
    """Persist and retrieve named saved queries."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else config_dir() / "queries.json"

    def load(self) -> list[Query]:
        return load_queries(self._path)

    def save_all(self, queries: list[Query]) -> None:
        dump_queries(queries, self._path)

    def add(self, query: Query) -> None:
        """Add ``query``, replacing any existing one with the same name."""
        queries = [q for q in self.load() if q.name != query.name]
        queries.append(query)
        self.save_all(queries)

    def delete(self, name: str) -> None:
        self.save_all([q for q in self.load() if q.name != name])


class BatchTemplateStore:
    """Persist and retrieve named batch-processing templates."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else config_dir() / "batch_templates.json"

    def load(self) -> list[BatchTemplate]:
        return load_templates(self._path)

    def save_all(self, templates: list[BatchTemplate]) -> None:
        dump_templates(templates, self._path)

    def add(self, template: BatchTemplate) -> None:
        """Add ``template``, replacing any existing one with the same name."""
        templates = [t for t in self.load() if t.name != template.name]
        templates.append(template)
        self.save_all(templates)

    def delete(self, name: str) -> None:
        self.save_all([t for t in self.load() if t.name != name])


class CpcConfigStore:
    """Persist the CPC data-source config in the **project** (default ``./cpc_config.json``).

    Kept in the working folder (not the app-config dir) so it travels with the project and can be
    shared/committed — it holds only the API-key **env-var name**, never the key itself.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else Path.cwd() / CPC_CONFIG_FILENAME

    def path(self) -> Path:
        return self._path

    def load(self) -> CpcConfig:
        return load_config(self._path)

    def save(self, config: CpcConfig) -> None:
        save_config(config, self._path)


class EntityMemoryStore:
    """Persist the normalization entity memory (canonical names + learned aliases).

    The active memory lives at a **relocatable** path — by default ``<cwd>/entities.json`` (the
    project folder, so the deduplicated memory is versionable and portable alongside the data). A
    small pointer file under the app-config dir remembers the chosen location across sessions, so
    :meth:`relocate` can move the memory to any project file and reopen it there next time.
    """

    def __init__(self, path: Path | None = None, *, pointer: Path | None = None) -> None:
        self._pointer = pointer if pointer is not None else config_dir() / "entity_location.json"
        if path is not None:
            self._path = path
        else:
            self._path = self._read_pointer() or (Path.cwd() / "entities.json")

    def _read_pointer(self) -> Path | None:
        if not self._pointer.is_file():
            return None
        try:
            raw = json.loads(self._pointer.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        stored = raw.get("path")
        return Path(str(stored)) if stored else None

    def _write_pointer(self) -> None:
        self._pointer.parent.mkdir(parents=True, exist_ok=True)
        self._pointer.write_text(json.dumps({"path": str(self._path)}, indent=2), encoding="utf-8")

    @property
    def path(self) -> Path:
        return self._path

    def relocate(self, new_path: Path, *, move: bool = True) -> None:
        """Point the active memory at ``new_path`` (persisted); carry current content over."""
        if move and self._path.is_file() and self._path != new_path:
            self.load().save(new_path)  # carry the current memory to its new home
        self._path = new_path
        self._write_pointer()

    def load(self) -> EntityMemory:
        return EntityMemory.load(self._path)

    def save(self, memory: EntityMemory) -> None:
        memory.save(self._path)

    def clear(self) -> None:
        """Reset the active memory to empty (discards learned canonicals/aliases)."""
        EntityMemory().save(self._path)


class UiStateStore:
    """Remembers last-used dialog directories (app-config JSON, stateless per call).

    Keys are free-form ("input", "output", "reference"); unknown or invalid entries read as "",
    which the dialogs treat as "no preference" (Qt then falls back to its default).
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else config_dir() / "ui_state.json"

    def _load(self) -> dict[str, str]:
        if not self._path.is_file():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        dirs = raw.get("last_dirs", {})
        return {str(k): str(v) for k, v in dirs.items()} if isinstance(dirs, dict) else {}

    def last_dir(self, key: str) -> str:
        """The last directory recorded for ``key`` ("" when unset or no longer a directory)."""
        value = self._load().get(key, "")
        return value if value and Path(value).is_dir() else ""

    def set_last_dir(self, key: str, value: str) -> None:
        """Record ``value`` as the last directory for ``key`` (blank values are ignored)."""
        if not value:
            return
        dirs = self._load()
        dirs[key] = value
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({"last_dirs": dirs}, indent=2), encoding="utf-8")
