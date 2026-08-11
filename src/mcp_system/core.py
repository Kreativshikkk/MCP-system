"""Environment lifecycle and isolated service provisioning."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import threading
from typing import Any, Iterator, Mapping

from .errors import (
    ConfigurationError,
    EnvironmentNotFoundError,
    EnvironmentNotReadyError,
    ServiceNotFoundError,
    SnapshotNotFoundError,
    TemplateNotFoundError,
    TemplateNotReadyError,
)
from .git_storage import GitDataPlaneStorage, GitServiceDataPlane
from .config import load_environment_spec, load_template_spec
from .models import (
    Environment,
    EnvironmentSpec,
    EnvironmentStatus,
    EnvironmentSnapshot,
    OperationRecord,
    OperationStatus,
    ServiceInstance,
    ServiceInstanceSpec,
    ServiceStatus,
    SnapshotDiff,
    StoredTemplate,
    Template,
    TemplateSpec,
    TemplateStatus,
)
from .plugins import PluginRegistry, RelationalSession, ServicePlugin
from .runtime import (
    Clock,
    IdGenerator,
    OperationIdGenerator,
    SystemClock,
    UUIDGenerator,
)
from .service_storage import ServiceStorageBackend, SQLiteServiceStorage
from .storage.base import ControlPlaneStore
from .storage.sqlite import SQLiteControlPlane


class MCPSystem:
    """Persistent local runtime for isolated service replicas."""

    def __init__(
        self,
        data_root: Path,
        registry: PluginRegistry,
        *,
        control_plane: ControlPlaneStore | None = None,
        service_storage: ServiceStorageBackend | None = None,
        git_storage: GitDataPlaneStorage | None = None,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        operation_ids: OperationIdGenerator | None = None,
    ) -> None:
        self.data_root = data_root.resolve()
        self.environments_root = self.data_root / "environments"
        self.registry = registry
        self.clock = clock or SystemClock()
        self.ids = ids or UUIDGenerator()
        self.operation_ids = operation_ids or UUIDGenerator()
        self._state_lock = threading.RLock()
        self.control_plane = control_plane or SQLiteControlPlane(
            self.data_root / "control.sqlite3"
        )
        self.service_storage = service_storage or SQLiteServiceStorage(self.data_root)
        self.git_storage = git_storage or GitDataPlaneStorage(self.data_root)

        self.environments_root.mkdir(parents=True, exist_ok=True)
        (self.data_root / "snapshots").mkdir(parents=True, exist_ok=True)
        self.control_plane.initialize()
        self.control_plane.recover_interrupted_provisioning(self.clock.now())

    @classmethod
    def with_postgres(
        cls,
        data_root: Path,
        registry: PluginRegistry,
        dsn: str,
        *,
        control_schema: str = "mcp_control",
        storage_namespace: str = "mcp",
        git_storage: GitDataPlaneStorage | None = None,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        operation_ids: OperationIdGenerator | None = None,
    ) -> "MCPSystem":
        from .storage.postgres import PostgresControlPlane
        from .storage.postgres_service import PostgresServiceStorage

        return cls(
            data_root,
            registry,
            control_plane=PostgresControlPlane(dsn, schema=control_schema),
            service_storage=PostgresServiceStorage(
                dsn, namespace=storage_namespace
            ),
            git_storage=git_storage,
            clock=clock,
            ids=ids,
            operation_ids=operation_ids,
        )

    def create_environment(self, spec: EnvironmentSpec) -> Environment:
        resolved_plugins = self._validate_and_resolve(spec)
        environment_id = self.ids.new_environment_id()
        service_paths = {
            service.instance_id: self.service_storage.build_locator(
                environment_id, service.instance_id
            )
            for service in spec.services
        }

        now = self.clock.now()
        self.control_plane.create_environment_record(
            environment_id, spec, service_paths, now
        )
        current_service: ServiceInstanceSpec | None = None
        try:
            for service, plugin in zip(spec.services, resolved_plugins, strict=True):
                current_service = service
                self.service_storage.provision(
                    service_paths[service.instance_id], plugin, service.seed
                )
                if self._has_git_data_plane(plugin):
                    self.git_storage.provision(
                        self.git_storage.build_locator(
                            environment_id, service.instance_id
                        )
                    )
                self.control_plane.set_service_status(
                    environment_id,
                    service.instance_id,
                    ServiceStatus.READY,
                    self.clock.now(),
                )

            self.control_plane.set_environment_status(
                environment_id, EnvironmentStatus.READY, self.clock.now()
            )
        except Exception as exc:
            if current_service is not None:
                self.control_plane.set_service_status(
                    environment_id,
                    current_service.instance_id,
                    ServiceStatus.FAILED,
                    self.clock.now(),
                )
            self.control_plane.set_environment_status(
                environment_id,
                EnvironmentStatus.FAILED,
                self.clock.now(),
                failure_reason=f"{type(exc).__name__}: {exc}",
            )
            raise

        return self.require_environment(environment_id)

    def create_environment_from_toml(self, path: Path) -> Environment:
        return self.create_environment(load_environment_spec(path))

    def create_template_from_toml(self, path: Path) -> Template:
        return self.create_template(load_template_spec(path))

    def create_template(self, spec: TemplateSpec) -> Template:
        environment_spec = spec.as_environment_spec()
        resolved_plugins = self._validate_and_resolve(environment_spec)
        service_paths = {
            service.instance_id: self.service_storage.build_template_locator(
                spec.template_id, service.instance_id
            )
            for service in spec.services
        }
        self.control_plane.create_template_record(
            spec, service_paths, self.clock.now()
        )

        current_service: ServiceInstanceSpec | None = None
        try:
            for service, plugin in zip(spec.services, resolved_plugins, strict=True):
                current_service = service
                self.service_storage.provision(
                    service_paths[service.instance_id], plugin, service.seed
                )
                if self._has_git_data_plane(plugin):
                    self.git_storage.provision(
                        self.git_storage.build_template_locator(
                            spec.template_id, service.instance_id
                        )
                    )
                self.control_plane.set_template_service_status(
                    spec.template_id,
                    service.instance_id,
                    ServiceStatus.READY,
                    self.clock.now(),
                )
            self.control_plane.set_template_status(
                spec.template_id, TemplateStatus.READY, self.clock.now()
            )
        except Exception as exc:
            if current_service is not None:
                self.control_plane.set_template_service_status(
                    spec.template_id,
                    current_service.instance_id,
                    ServiceStatus.FAILED,
                    self.clock.now(),
                )
            self.control_plane.set_template_status(
                spec.template_id,
                TemplateStatus.FAILED,
                self.clock.now(),
                failure_reason=f"{type(exc).__name__}: {exc}",
            )
            raise

        return self.require_template(spec.template_id).template

    def create_environment_from_template(
        self, template_id: str, *, name: str | None = None
    ) -> Environment:
        stored = self.require_template(template_id)
        if stored.template.status is not TemplateStatus.READY:
            raise TemplateNotReadyError(
                f"template {template_id!r} is {stored.template.status}"
            )
        if any(service.status is not ServiceStatus.READY for service in stored.services):
            raise TemplateNotReadyError(
                f"template {template_id!r} contains a non-ready service"
            )

        spec = stored.to_spec().as_environment_spec(name=name)
        self._validate_and_resolve(spec)
        environment_id = self.ids.new_environment_id()
        target_paths = {
            service.instance_id: self.service_storage.build_locator(
                environment_id, service.instance_id
            )
            for service in stored.services
        }
        self.control_plane.create_environment_record(
            environment_id,
            spec,
            target_paths,
            self.clock.now(),
            template_id=template_id,
        )

        current_instance_id: str | None = None
        try:
            for service in stored.services:
                current_instance_id = service.instance_id
                plugin = self.registry.resolve(
                    service.plugin_id, service.plugin_version
                )
                self.service_storage.clone(
                    service.database_path,
                    target_paths[service.instance_id],
                    plugin,
                )
                if self._has_git_data_plane(plugin):
                    source_git_locator = self.git_storage.build_template_locator(
                        template_id, service.instance_id
                    )
                    # Templates created before the Git capability existed have
                    # no data-plane directory. Their seed never contained Git
                    # objects, so an empty one is the lossless migration.
                    if not self.git_storage.exists(source_git_locator):
                        self.git_storage.provision(source_git_locator)
                    self.git_storage.clone(
                        source_git_locator,
                        self.git_storage.build_locator(
                            environment_id, service.instance_id
                        ),
                    )
                self.control_plane.set_service_status(
                    environment_id,
                    service.instance_id,
                    ServiceStatus.READY,
                    self.clock.now(),
                )
            self.control_plane.set_environment_status(
                environment_id, EnvironmentStatus.READY, self.clock.now()
            )
        except Exception as exc:
            if current_instance_id is not None:
                self.control_plane.set_service_status(
                    environment_id,
                    current_instance_id,
                    ServiceStatus.FAILED,
                    self.clock.now(),
                )
            self.control_plane.set_environment_status(
                environment_id,
                EnvironmentStatus.FAILED,
                self.clock.now(),
                failure_reason=f"{type(exc).__name__}: {exc}",
            )
            raise

        return self.require_environment(environment_id)

    def require_environment(self, environment_id: str) -> Environment:
        environment = self.control_plane.get_environment(environment_id)
        if environment is None:
            raise EnvironmentNotFoundError(
                f"environment {environment_id!r} does not exist"
            )
        return environment

    def list_environments(self) -> tuple[Environment, ...]:
        return tuple(self.control_plane.list_environments())

    def require_template(self, template_id: str) -> StoredTemplate:
        template = self.control_plane.get_template(template_id)
        if template is None:
            raise TemplateNotFoundError(f"template {template_id!r} does not exist")
        return template

    def list_templates(self) -> tuple[Template, ...]:
        return tuple(self.control_plane.list_templates())

    def list_mcp_surfaces(self, environment_id: str) -> tuple[str, ...]:
        self.require_environment(environment_id)
        return self.control_plane.list_mcp_surfaces(environment_id)

    def list_services(self, environment_id: str) -> tuple[ServiceInstance, ...]:
        self.require_environment(environment_id)
        return tuple(self.control_plane.list_services(environment_id))

    def list_operations(
        self,
        environment_id: str,
        *,
        service_instance_id: str | None = None,
        limit: int = 100,
    ) -> tuple[OperationRecord, ...]:
        """Read the durable provider-operation timeline for an environment."""
        self.require_environment(environment_id)
        if not 1 <= limit <= 1000:
            raise ConfigurationError("operation list limit must be between 1 and 1000")
        if service_instance_id is not None:
            service = self.control_plane.get_service(
                environment_id, service_instance_id
            )
            if service is None:
                raise ServiceNotFoundError(
                    f"service {service_instance_id!r} is not in environment "
                    f"{environment_id!r}"
                )
        return tuple(
            self.control_plane.list_operations(
                environment_id,
                service_instance_id=service_instance_id,
                limit=limit,
            )
        )

    def snapshot_environment(
        self, environment_id: str, *, name: str | None = None
    ) -> EnvironmentSnapshot:
        """Capture an immutable, cloneable SQL + Git state of one environment."""
        with self._state_lock:
            environment = self.require_environment(environment_id)
            if environment.status is not EnvironmentStatus.READY:
                raise EnvironmentNotReadyError(
                    f"environment {environment_id!r} is {environment.status}"
                )
            services = self.list_services(environment_id)
            snapshot_id = self.ids.new_environment_id()
            created_at = self.clock.now()
            operation_count, operation_cursor = self.control_plane.get_operation_cursor(
                environment_id
            )
            service_records: list[dict[str, Any]] = []
            for service in services:
                plugin = self.registry.resolve(
                    service.plugin_id, service.plugin_version
                )
                snapshot_locator = self.service_storage.build_snapshot_locator(
                    snapshot_id, service.instance_id
                )
                self.service_storage.clone(
                    service.database_path, snapshot_locator, plugin
                )
                git_locator = None
                if self._has_git_data_plane(plugin):
                    git_locator = self.git_storage.build_snapshot_locator(
                        snapshot_id, service.instance_id
                    )
                    self.git_storage.clone(
                        self.git_storage.build_locator(
                            environment_id, service.instance_id
                        ),
                        git_locator,
                    )
                service_records.append(
                    {
                        "instanceId": service.instance_id,
                        "pluginId": service.plugin_id,
                        "pluginVersion": service.plugin_version,
                        "databaseLocator": snapshot_locator,
                        "gitLocator": git_locator,
                    }
                )
            manifest = {
                "formatVersion": 1,
                "id": snapshot_id,
                "sourceEnvironmentId": environment_id,
                "name": name or f"Snapshot of {environment.name}",
                "createdAt": created_at.isoformat(),
                "operationCount": operation_count,
                "operationCursor": operation_cursor,
                "mcpSurfaces": list(self.list_mcp_surfaces(environment_id)),
                "services": service_records,
            }
            path = self._snapshot_manifest_path(snapshot_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(path)
            return self._snapshot_from_manifest(manifest)

    def create_environment_from_snapshot(
        self, snapshot_id: str, *, name: str | None = None
    ) -> Environment:
        """Create an independent mutable environment from an immutable snapshot."""
        with self._state_lock:
            manifest = self._load_snapshot_manifest(snapshot_id)
            services = tuple(
                ServiceInstanceSpec(
                    instance_id=record["instanceId"],
                    plugin_id=record["pluginId"],
                    plugin_version=record["pluginVersion"],
                    seed={},
                )
                for record in manifest["services"]
            )
            for service in services:
                self.registry.resolve(service.plugin_id, service.plugin_version)
            spec = EnvironmentSpec(
                name=name or f"Clone of {manifest['name']}",
                services=services,
                mcp_surfaces=tuple(manifest["mcpSurfaces"]),
            )
            environment_id = self.ids.new_environment_id()
            target_paths = {
                service.instance_id: self.service_storage.build_locator(
                    environment_id, service.instance_id
                )
                for service in services
            }
            self.control_plane.create_environment_record(
                environment_id,
                spec,
                target_paths,
                self.clock.now(),
                snapshot_id=snapshot_id,
            )
            try:
                records = {
                    record["instanceId"]: record for record in manifest["services"]
                }
                for service in services:
                    record = records[service.instance_id]
                    plugin = self.registry.resolve(
                        service.plugin_id, service.plugin_version
                    )
                    self.service_storage.clone(
                        record["databaseLocator"],
                        target_paths[service.instance_id],
                        plugin,
                    )
                    if self._has_git_data_plane(plugin):
                        if not record["gitLocator"]:
                            raise ConfigurationError(
                                f"snapshot {snapshot_id!r} lacks Git state for "
                                f"{service.instance_id!r}"
                            )
                        self.git_storage.clone(
                            record["gitLocator"],
                            self.git_storage.build_locator(
                                environment_id, service.instance_id
                            ),
                        )
                    self.control_plane.set_service_status(
                        environment_id,
                        service.instance_id,
                        ServiceStatus.READY,
                        self.clock.now(),
                    )
                self.control_plane.set_environment_status(
                    environment_id, EnvironmentStatus.READY, self.clock.now()
                )
            except Exception as exc:
                self.control_plane.set_environment_status(
                    environment_id,
                    EnvironmentStatus.FAILED,
                    self.clock.now(),
                    failure_reason=f"{type(exc).__name__}: {exc}",
                )
                raise
            return self.require_environment(environment_id)

    def diff_snapshots(
        self, before_snapshot_id: str, after_snapshot_id: str
    ) -> SnapshotDiff:
        """Return a deterministic relational and Git state diff."""
        before = self._load_snapshot_manifest(before_snapshot_id)
        after = self._load_snapshot_manifest(after_snapshot_id)
        before_services = {
            item["instanceId"]: item for item in before["services"]
        }
        after_services = {item["instanceId"]: item for item in after["services"]}
        service_diffs: dict[str, Any] = {}
        for instance_id in sorted(set(before_services) | set(after_services)):
            left = before_services.get(instance_id)
            right = after_services.get(instance_id)
            if left is None or right is None:
                service_diffs[instance_id] = {
                    "change": "added" if left is None else "deleted"
                }
                continue
            if (left["pluginId"], left["pluginVersion"]) != (
                right["pluginId"], right["pluginVersion"]
            ):
                service_diffs[instance_id] = {
                    "change": "plugin_changed",
                    "before": [left["pluginId"], left["pluginVersion"]],
                    "after": [right["pluginId"], right["pluginVersion"]],
                }
                continue
            relational = _diff_relational_states(
                self.service_storage.inspect(left["databaseLocator"]),
                self.service_storage.inspect(right["databaseLocator"]),
            )
            git = None
            if left["gitLocator"] or right["gitLocator"]:
                git = _diff_git_states(
                    self.git_storage.inspect(left["gitLocator"])
                    if left["gitLocator"] else {},
                    self.git_storage.inspect(right["gitLocator"])
                    if right["gitLocator"] else {},
                )
            service_diffs[instance_id] = {
                "pluginId": left["pluginId"],
                "pluginVersion": left["pluginVersion"],
                "relational": relational,
                "git": git,
            }
        metadata = {
            "mcpSurfaces": {
                "before": before["mcpSurfaces"],
                "after": after["mcpSurfaces"],
            },
            "operationCursor": {
                "before": before["operationCursor"],
                "after": after["operationCursor"],
                "countDelta": after["operationCount"] - before["operationCount"],
            },
        }
        return SnapshotDiff(
            before_snapshot_id=before_snapshot_id,
            after_snapshot_id=after_snapshot_id,
            metadata=metadata,
            services=service_diffs,
        )

    def require_snapshot(self, snapshot_id: str) -> EnvironmentSnapshot:
        return self._snapshot_from_manifest(
            self._load_snapshot_manifest(snapshot_id)
        )

    def _snapshot_manifest_path(self, snapshot_id: str) -> Path:
        if not snapshot_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in snapshot_id):
            raise SnapshotNotFoundError(f"snapshot {snapshot_id!r} does not exist")
        return self.data_root / "snapshots" / snapshot_id / "manifest.json"

    def _load_snapshot_manifest(self, snapshot_id: str) -> dict[str, Any]:
        path = self._snapshot_manifest_path(snapshot_id)
        if not path.is_file():
            raise SnapshotNotFoundError(f"snapshot {snapshot_id!r} does not exist")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("formatVersion") != 1 or manifest.get("id") != snapshot_id:
            raise ConfigurationError(f"snapshot {snapshot_id!r} manifest is invalid")
        return manifest

    @staticmethod
    def _snapshot_from_manifest(manifest: Mapping[str, Any]) -> EnvironmentSnapshot:
        return EnvironmentSnapshot(
            id=manifest["id"],
            source_environment_id=manifest["sourceEnvironmentId"],
            name=manifest["name"],
            created_at=datetime.fromisoformat(manifest["createdAt"]),
            operation_count=manifest["operationCount"],
            operation_cursor=manifest["operationCursor"],
            mcp_surfaces=tuple(manifest["mcpSurfaces"]),
        )

    def invoke_service_operation(
        self,
        environment_id: str,
        instance_id: str,
        *,
        actor: str,
        transport: str,
        operation: str,
        arguments: dict[str, object],
    ) -> object:
        with self._state_lock:
            return self._invoke_service_operation(
                environment_id, instance_id, actor=actor, transport=transport,
                operation=operation, arguments=arguments,
            )

    def _invoke_service_operation(
        self,
        environment_id: str,
        instance_id: str,
        *,
        actor: str,
        transport: str,
        operation: str,
        arguments: dict[str, object],
    ) -> object:
        """Invoke and durably audit one transport-neutral provider operation."""
        service = self.control_plane.get_service(environment_id, instance_id)
        if service is None:
            raise ServiceNotFoundError(
                f"service {instance_id!r} is not in environment {environment_id!r}"
            )
        request = _json_copy(arguments, "operation request")
        operation_id = self.operation_ids.new_operation_id()
        self.control_plane.begin_operation(
            OperationRecord(
                id=operation_id,
                environment_id=environment_id,
                service_instance_id=instance_id,
                plugin_id=service.plugin_id,
                actor=actor,
                transport=transport,
                operation=operation,
                request=request,
                status=OperationStatus.RUNNING,
                started_at=self.clock.now(),
            )
        )
        try:
            with self.open_service_operations(
                environment_id, instance_id, actor=actor
            ) as operations:
                result = getattr(operations, operation)(**arguments)
                persisted_result = _json_copy(result, "operation result")
        except Exception as exc:
            error: dict[str, object] = {
                "type": getattr(exc, "error", type(exc).__name__),
                "message": str(exc),
            }
            status_code = getattr(exc, "status_code", None)
            if status_code is not None:
                error["status"] = status_code
            self.control_plane.complete_operation(
                operation_id,
                OperationStatus.FAILED,
                self.clock.now(),
                error=error,
            )
            raise
        self.control_plane.complete_operation(
            operation_id,
            OperationStatus.SUCCEEDED,
            self.clock.now(),
            result=persisted_result,
        )
        return result

    @contextmanager
    def open_service_database(
        self, environment_id: str, instance_id: str
    ) -> Iterator[RelationalSession]:
        environment = self.require_environment(environment_id)
        if environment.status is not EnvironmentStatus.READY:
            raise EnvironmentNotReadyError(
                f"environment {environment_id!r} is {environment.status}"
            )

        service = self.control_plane.get_service(environment_id, instance_id)
        if service is None:
            raise ServiceNotFoundError(
                f"service {instance_id!r} is not in environment {environment_id!r}"
            )
        if service.status is not ServiceStatus.READY:
            raise EnvironmentNotReadyError(
                f"service {instance_id!r} is {service.status}"
            )

        with self.service_storage.open(service.database_path) as session:
            yield session

    @contextmanager
    def open_service_operations(
        self,
        environment_id: str,
        instance_id: str,
        *,
        actor: str,
        now: object | None = None,
    ) -> Iterator[object]:
        """Open provider operations with every configured local data plane."""
        service = self.control_plane.get_service(environment_id, instance_id)
        if service is None:
            raise ServiceNotFoundError(
                f"service {instance_id!r} is not in environment {environment_id!r}"
            )
        plugin = self.registry.resolve(service.plugin_id, service.plugin_version)
        git_data_plane = None
        if self._has_git_data_plane(plugin):
            git_data_plane = self.open_git_data_plane(
                environment_id, instance_id
            ).transaction()
        try:
            with self.open_service_database(environment_id, instance_id) as session:
                yield plugin.create_operations(
                    session,
                    actor=actor,
                    now=now,
                    git_data_plane=git_data_plane,
                )
        except Exception:
            if git_data_plane is not None:
                git_data_plane.rollback()
            raise
        else:
            if git_data_plane is not None:
                git_data_plane.commit()

    def open_git_data_plane(
        self, environment_id: str, instance_id: str
    ) -> GitServiceDataPlane:
        environment = self.require_environment(environment_id)
        if environment.status is not EnvironmentStatus.READY:
            raise EnvironmentNotReadyError(
                f"environment {environment_id!r} is {environment.status}"
            )
        service = self.control_plane.get_service(environment_id, instance_id)
        if service is None:
            raise ServiceNotFoundError(
                f"service {instance_id!r} is not in environment {environment_id!r}"
            )
        plugin = self.registry.resolve(service.plugin_id, service.plugin_version)
        if not self._has_git_data_plane(plugin):
            raise ServiceNotFoundError(
                f"service {instance_id!r} does not provide a Git data plane"
            )
        return self.git_storage.open(
            self.git_storage.build_locator(environment_id, instance_id)
        )

    def _validate_and_resolve(
        self, spec: EnvironmentSpec
    ) -> tuple[ServicePlugin, ...]:
        resolved: list[ServicePlugin] = []
        for service in spec.services:
            try:
                json.dumps(service.seed, sort_keys=True, separators=(",", ":"))
            except (TypeError, ValueError) as exc:
                raise ConfigurationError(
                    f"seed for service {service.instance_id!r} is not JSON serializable"
                ) from exc
            plugin = self.registry.resolve(
                service.plugin_id, service.plugin_version
            )
            plugin.validate_bootstrap(service.seed)
            resolved.append(plugin)
        return tuple(resolved)

    @staticmethod
    def _has_git_data_plane(plugin: ServicePlugin) -> bool:
        return "git_data_plane" in plugin.manifest.capabilities


def _json_copy(value: object, context: str) -> object:
    try:
        return json.loads(json.dumps(value, sort_keys=True, separators=(",", ":")))
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{context} is not JSON serializable") from exc


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"$bytes": value.hex()}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _row_key(row: Mapping[str, Any], primary_key: list[str], columns: list[str]) -> str:
    fields = primary_key or columns
    return json.dumps(
        [_jsonable(row.get(field)) for field in fields],
        sort_keys=True,
        separators=(",", ":"),
    )


def _diff_relational_states(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> Mapping[str, Any]:
    tables: dict[str, Any] = {}
    totals = {"inserted": 0, "deleted": 0, "updated": 0}
    for table in sorted(set(before) | set(after)):
        left = before.get(table)
        right = after.get(table)
        if left is None or right is None:
            rows = (right or left)["rows"]
            kind = "inserted" if left is None else "deleted"
            normalized = [_jsonable(row) for row in rows]
            tables[table] = {kind: normalized, "deleted" if kind == "inserted" else "inserted": [], "updated": []}
            totals[kind] += len(normalized)
            continue
        columns = list(right["columns"])
        primary_key = list(right["primaryKey"])
        left_rows = {
            _row_key(row, primary_key, columns): _jsonable(row)
            for row in left["rows"]
        }
        right_rows = {
            _row_key(row, primary_key, columns): _jsonable(row)
            for row in right["rows"]
        }
        inserted = [right_rows[key] for key in sorted(set(right_rows) - set(left_rows))]
        deleted = [left_rows[key] for key in sorted(set(left_rows) - set(right_rows))]
        updated = [
            {"key": json.loads(key), "before": left_rows[key], "after": right_rows[key]}
            for key in sorted(set(left_rows) & set(right_rows))
            if left_rows[key] != right_rows[key]
        ]
        if inserted or deleted or updated:
            tables[table] = {
                "primaryKey": primary_key,
                "inserted": inserted,
                "deleted": deleted,
                "updated": updated,
            }
            totals["inserted"] += len(inserted)
            totals["deleted"] += len(deleted)
            totals["updated"] += len(updated)
    return {"summary": totals, "tables": tables}


def _diff_git_states(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> Mapping[str, Any]:
    repositories: dict[str, Any] = {}
    for repository in sorted(set(before) | set(after)):
        left_refs = before.get(repository, {}).get("refs", {})
        right_refs = after.get(repository, {}).get("refs", {})
        added = {key: right_refs[key] for key in sorted(set(right_refs) - set(left_refs))}
        deleted = {key: left_refs[key] for key in sorted(set(left_refs) - set(right_refs))}
        updated = {
            key: {"before": left_refs[key], "after": right_refs[key]}
            for key in sorted(set(left_refs) & set(right_refs))
            if left_refs[key] != right_refs[key]
        }
        if added or deleted or updated:
            repositories[repository] = {
                "added": added,
                "deleted": deleted,
                "updated": updated,
            }
    return {"repositories": repositories}
