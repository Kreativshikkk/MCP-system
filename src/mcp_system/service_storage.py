"""Replaceable persistence backend for isolated service state."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Any, ContextManager, Iterator, Mapping, Protocol

from .errors import ConfigurationError
from .plugins import PluginRegistry, RelationalSession, ServicePlugin


class ServiceStorageBackend(Protocol):
    @property
    def kind(self) -> str: ...

    def build_locator(self, environment_id: str, instance_id: str) -> str: ...

    def build_template_locator(self, template_id: str, instance_id: str) -> str: ...

    def build_snapshot_locator(self, snapshot_id: str, instance_id: str) -> str: ...

    def provision(
        self,
        locator: str,
        plugin: ServicePlugin,
        seed: Mapping[str, Any],
    ) -> None: ...

    def open(self, locator: str) -> ContextManager[RelationalSession]: ...

    def clone(
        self,
        source_locator: str,
        target_locator: str,
        plugin: ServicePlugin,
    ) -> None: ...

    def inspect(self, locator: str) -> Mapping[str, Any]: ...


class SQLiteServiceStorage:
    """File-backed reference backend; one database per service instance."""

    kind = "sqlite"

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root.resolve()
        self.environments_root = self.data_root / "environments"
        self.templates_root = self.data_root / "templates"
        self.snapshots_root = self.data_root / "snapshots"
        self.environments_root.mkdir(parents=True, exist_ok=True)
        self.templates_root.mkdir(parents=True, exist_ok=True)
        self.snapshots_root.mkdir(parents=True, exist_ok=True)

    def build_locator(self, environment_id: str, instance_id: str) -> str:
        return str(
            Path("environments") / environment_id / f"{instance_id}.sqlite3"
        )

    def build_template_locator(self, template_id: str, instance_id: str) -> str:
        return str(Path("templates") / template_id / f"{instance_id}.sqlite3")

    def build_snapshot_locator(self, snapshot_id: str, instance_id: str) -> str:
        return str(Path("snapshots") / snapshot_id / f"{instance_id}.sqlite3")

    def provision(
        self,
        locator: str,
        plugin: ServicePlugin,
        seed: Mapping[str, Any],
    ) -> None:
        path = self._resolve(locator)
        path.parent.mkdir(parents=True, exist_ok=True)
        migrations = PluginRegistry.validate_migrations(plugin, self.kind)

        connection = sqlite3.connect(path)
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE _mcp_plugin_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            for migration in migrations:
                for statement in migration.statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO _mcp_plugin_migrations(version) VALUES (?)",
                    (migration.version,),
                )
            plugin.seed(connection, seed)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def open(self, locator: str) -> Iterator[RelationalSession]:
        path = self._resolve(locator)
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def clone(
        self,
        source_locator: str,
        target_locator: str,
        plugin: ServicePlugin,
    ) -> None:
        source_path = self._resolve(source_locator)
        target_path = self._resolve(target_locator)
        if not source_path.exists():
            raise ConfigurationError(
                f"service storage source does not exist: {source_locator}"
            )
        if target_path.exists():
            raise ConfigurationError(
                f"service storage target already exists: {target_locator}"
            )
        target_path.parent.mkdir(parents=True, exist_ok=True)

        source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
        target = sqlite3.connect(target_path)
        try:
            source.backup(target)
            target.commit()
        except Exception:
            target.rollback()
            raise
        finally:
            target.close()
            source.close()

    def inspect(self, locator: str) -> Mapping[str, Any]:
        path = self._resolve(locator)
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name <> '_mcp_plugin_migrations' ORDER BY name").fetchall()]
            result: dict[str, Any] = {}
            for table in tables:
                quoted = '"' + table.replace('"', '""') + '"'
                columns = connection.execute(f"PRAGMA table_info({quoted})").fetchall()
                names = [row["name"] for row in columns]
                primary_key = [row["name"] for row in sorted(columns, key=lambda item: item["pk"]) if row["pk"]]
                rows = [dict(row) for row in connection.execute(f"SELECT * FROM {quoted}").fetchall()]
                result[table] = {"columns": names, "primaryKey": primary_key, "rows": rows}
            return result
        finally:
            connection.close()

    def _resolve(self, locator: str) -> Path:
        path = (self.data_root / locator).resolve()
        if not (
            path.is_relative_to(self.environments_root)
            or path.is_relative_to(self.templates_root)
            or path.is_relative_to(self.snapshots_root)
        ):
            raise ConfigurationError("service storage locator escapes the data root")
        return path
