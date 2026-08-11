"""Versioned service plugin contract and in-process registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from .errors import ConfigurationError, PluginNotFoundError
from .models import require_identifier


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    statements: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.version <= 0:
            raise ConfigurationError("migration version must be positive")
        if not self.statements or any(not sql.strip() for sql in self.statements):
            raise ConfigurationError("migration must contain non-empty SQL statements")


@dataclass(frozen=True, slots=True)
class PluginManifest:
    plugin_id: str
    version: str
    capabilities: tuple[str, ...] = ()
    display_name: str | None = None
    contract_source: str | None = None
    contract_revision: str | None = None
    api_version: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.plugin_id, "plugin_id")
        if not self.version.strip():
            raise ConfigurationError("plugin version must not be empty")
        for capability in self.capabilities:
            require_identifier(capability, "capability")


class ServicePlugin(Protocol):
    """A local service implementation provisioned into an isolated database."""

    @property
    def manifest(self) -> PluginManifest: ...

    def migrations(self, storage_kind: str) -> Sequence[Migration]: ...

    def validate_bootstrap(self, config: Mapping[str, Any]) -> None:
        """Validate bootstrap configuration before allocating physical storage."""
        ...

    def seed(self, session: "RelationalSession", config: Mapping[str, Any]) -> None:
        """Seed a new service database without committing the transaction."""
        ...

    def create_operations(
        self,
        session: "RelationalSession",
        *,
        actor: str,
        now: Any | None = None,
        git_data_plane: Any | None = None,
    ) -> Any:
        """Bind provider domain operations to one service transaction."""
        ...


class RelationalResult(Protocol):
    def fetchone(self) -> Any: ...

    def fetchall(self) -> list[Any]: ...


class RelationalSession(Protocol):
    """Small SQL session surface shared by local relational backends."""

    def execute(
        self, statement: str, parameters: Sequence[Any] = ()
    ) -> RelationalResult: ...

    def executemany(
        self, statement: str, parameters: Sequence[Sequence[Any]]
    ) -> RelationalResult: ...


class PluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[tuple[str, str], ServicePlugin] = {}

    def register(self, plugin: ServicePlugin) -> None:
        key = (plugin.manifest.plugin_id, plugin.manifest.version)
        if key in self._plugins:
            raise ConfigurationError(
                f"plugin {key[0]!r} version {key[1]!r} is already registered"
            )
        self._plugins[key] = plugin

    def resolve(self, plugin_id: str, version: str) -> ServicePlugin:
        try:
            return self._plugins[(plugin_id, version)]
        except KeyError as exc:
            raise PluginNotFoundError(
                f"plugin {plugin_id!r} version {version!r} is not registered"
            ) from exc

    @staticmethod
    def validate_migrations(
        plugin: ServicePlugin, storage_kind: str
    ) -> tuple[Migration, ...]:
        migrations = tuple(plugin.migrations(storage_kind))
        versions = [migration.version for migration in migrations]
        if versions != sorted(versions) or len(versions) != len(set(versions)):
            raise ConfigurationError(
                f"plugin {plugin.manifest.plugin_id!r} migrations must have "
                "unique, ascending versions"
            )
        return migrations
