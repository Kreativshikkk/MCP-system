"""SQLite implementation of the persistent MCPSystem control plane."""

from __future__ import annotations

from datetime import datetime
import json
import sqlite3
from pathlib import Path

from ..models import (
    Environment,
    EnvironmentSpec,
    EnvironmentStatus,
    OperationRecord,
    OperationStatus,
    ServiceInstance,
    ServiceStatus,
    StoredTemplate,
    Template,
    TemplateService,
    TemplateSpec,
    TemplateStatus,
)


_CONTROL_PLANE_MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS environments (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS environment_services (
    environment_id TEXT NOT NULL REFERENCES environments(id),
    instance_id TEXT NOT NULL,
    plugin_id TEXT NOT NULL,
    plugin_version TEXT NOT NULL,
    status TEXT NOT NULL,
    database_path TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (environment_id, instance_id)
);

CREATE TABLE IF NOT EXISTS environment_mcp_surfaces (
    environment_id TEXT NOT NULL REFERENCES environments(id),
    surface_id TEXT NOT NULL,
    PRIMARY KEY (environment_id, surface_id)
);
"""

_CONTROL_PLANE_MIGRATION_2 = """
ALTER TABLE environments ADD COLUMN template_id TEXT;

CREATE TABLE templates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(name, version)
);

CREATE TABLE template_services (
    template_id TEXT NOT NULL REFERENCES templates(id),
    instance_id TEXT NOT NULL,
    plugin_id TEXT NOT NULL,
    plugin_version TEXT NOT NULL,
    status TEXT NOT NULL,
    database_path TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (template_id, instance_id)
);

CREATE TABLE template_mcp_surfaces (
    template_id TEXT NOT NULL REFERENCES templates(id),
    surface_id TEXT NOT NULL,
    PRIMARY KEY (template_id, surface_id)
);
"""

_CONTROL_PLANE_MIGRATION_3 = """
CREATE TABLE operations (
    id TEXT PRIMARY KEY,
    environment_id TEXT NOT NULL,
    service_instance_id TEXT NOT NULL,
    plugin_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    transport TEXT NOT NULL,
    operation_name TEXT NOT NULL,
    request_json TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT,
    error_json TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (environment_id, service_instance_id)
        REFERENCES environment_services(environment_id, instance_id)
);

CREATE INDEX operations_environment_timeline
    ON operations(environment_id, started_at, id);
"""

_CONTROL_PLANE_MIGRATION_4 = """
ALTER TABLE environments ADD COLUMN snapshot_id TEXT;
"""


class SQLiteControlPlane:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS control_plane_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            applied = {
                row[0]
                for row in connection.execute(
                    "SELECT version FROM control_plane_migrations"
                ).fetchall()
            }
            if 1 not in applied:
                connection.executescript(_CONTROL_PLANE_MIGRATION_1)
                connection.execute(
                    "INSERT INTO control_plane_migrations(version) VALUES (1)"
                )
            if 2 not in applied:
                connection.executescript(_CONTROL_PLANE_MIGRATION_2)
                connection.execute(
                    "INSERT INTO control_plane_migrations(version) VALUES (2)"
                )
            if 3 not in applied:
                connection.executescript(_CONTROL_PLANE_MIGRATION_3)
                connection.execute(
                    "INSERT INTO control_plane_migrations(version) VALUES (3)"
                )
            if 4 not in applied:
                connection.executescript(_CONTROL_PLANE_MIGRATION_4)
                connection.execute("INSERT INTO control_plane_migrations(version) VALUES (4)")

    def recover_interrupted_provisioning(self, at: datetime) -> None:
        timestamp = at.isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE environment_services
                   SET status = ?, updated_at = ?
                 WHERE status = ?
                """,
                (ServiceStatus.FAILED, timestamp, ServiceStatus.PROVISIONING),
            )
            connection.execute(
                """
                UPDATE environments
                   SET status = ?, failure_reason = ?, updated_at = ?
                 WHERE status = ?
                """,
                (
                    EnvironmentStatus.FAILED,
                    "provisioning was interrupted by a previous process",
                    timestamp,
                    EnvironmentStatus.PROVISIONING,
                ),
            )
            connection.execute(
                """
                UPDATE template_services
                   SET status = ?, updated_at = ?
                 WHERE status = ?
                """,
                (ServiceStatus.FAILED, timestamp, ServiceStatus.PROVISIONING),
            )
            connection.execute(
                """
                UPDATE templates
                   SET status = ?, failure_reason = ?, updated_at = ?
                 WHERE status = ?
                """,
                (
                    TemplateStatus.FAILED,
                    "provisioning was interrupted by a previous process",
                    timestamp,
                    TemplateStatus.PROVISIONING,
                ),
            )
            connection.execute(
                """
                UPDATE operations
                   SET status = ?, error_json = ?, completed_at = ?
                 WHERE status = ?
                """,
                (
                    OperationStatus.INTERRUPTED,
                    json.dumps(
                        {
                            "type": "interrupted",
                            "message": "operation was interrupted by a previous process",
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    timestamp,
                    OperationStatus.RUNNING,
                ),
            )

    def create_environment_record(
        self,
        environment_id: str,
        spec: EnvironmentSpec,
        service_paths: dict[str, str],
        at: datetime,
        template_id: str | None = None,
        snapshot_id: str | None = None,
    ) -> None:
        timestamp = at.isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO environments(
                    id, name, status, template_id, snapshot_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    environment_id,
                    spec.name,
                    EnvironmentStatus.PROVISIONING,
                    template_id,
                    snapshot_id,
                    timestamp,
                    timestamp,
                ),
            )
            connection.executemany(
                """
                INSERT INTO environment_services(
                    environment_id, instance_id, plugin_id, plugin_version,
                    status, database_path, config_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        environment_id,
                        service.instance_id,
                        service.plugin_id,
                        service.plugin_version,
                        ServiceStatus.PROVISIONING,
                        service_paths[service.instance_id],
                        json.dumps(
                            service.seed,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        timestamp,
                        timestamp,
                    )
                    for service in spec.services
                ],
            )
            connection.executemany(
                """
                INSERT INTO environment_mcp_surfaces(environment_id, surface_id)
                VALUES (?, ?)
                """,
                [(environment_id, surface) for surface in spec.mcp_surfaces],
            )

    def create_template_record(
        self,
        spec: TemplateSpec,
        service_paths: dict[str, str],
        at: datetime,
    ) -> None:
        timestamp = at.isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO templates(
                    id, name, version, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    spec.template_id,
                    spec.name,
                    spec.version,
                    TemplateStatus.PROVISIONING,
                    timestamp,
                    timestamp,
                ),
            )
            connection.executemany(
                """
                INSERT INTO template_services(
                    template_id, instance_id, plugin_id, plugin_version,
                    status, database_path, config_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        spec.template_id,
                        service.instance_id,
                        service.plugin_id,
                        service.plugin_version,
                        ServiceStatus.PROVISIONING,
                        service_paths[service.instance_id],
                        json.dumps(
                            service.seed,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        timestamp,
                        timestamp,
                    )
                    for service in spec.services
                ],
            )
            connection.executemany(
                """
                INSERT INTO template_mcp_surfaces(template_id, surface_id)
                VALUES (?, ?)
                """,
                [(spec.template_id, surface) for surface in spec.mcp_surfaces],
            )

    def set_environment_status(
        self,
        environment_id: str,
        status: EnvironmentStatus,
        at: datetime,
        failure_reason: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE environments
                   SET status = ?, failure_reason = ?, updated_at = ?
                 WHERE id = ?
                """,
                (status, failure_reason, at.isoformat(), environment_id),
            )

    def set_service_status(
        self,
        environment_id: str,
        instance_id: str,
        status: ServiceStatus,
        at: datetime,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE environment_services
                   SET status = ?, updated_at = ?
                 WHERE environment_id = ? AND instance_id = ?
                """,
                (status, at.isoformat(), environment_id, instance_id),
            )

    def set_template_status(
        self,
        template_id: str,
        status: TemplateStatus,
        at: datetime,
        failure_reason: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE templates
                   SET status = ?, failure_reason = ?, updated_at = ?
                 WHERE id = ?
                """,
                (status, failure_reason, at.isoformat(), template_id),
            )

    def set_template_service_status(
        self,
        template_id: str,
        instance_id: str,
        status: ServiceStatus,
        at: datetime,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE template_services
                   SET status = ?, updated_at = ?
                 WHERE template_id = ? AND instance_id = ?
                """,
                (status, at.isoformat(), template_id, instance_id),
            )

    def get_environment(self, environment_id: str) -> Environment | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM environments WHERE id = ?", (environment_id,)
            ).fetchone()
        return self._to_environment(row) if row else None

    def list_environments(self) -> tuple[Environment, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM environments ORDER BY created_at, id"
            ).fetchall()
        return tuple(self._to_environment(row) for row in rows)

    def get_service(
        self, environment_id: str, instance_id: str
    ) -> ServiceInstance | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM environment_services
                 WHERE environment_id = ? AND instance_id = ?
                """,
                (environment_id, instance_id),
            ).fetchone()
        if row is None:
            return None
        return ServiceInstance(
            environment_id=row["environment_id"],
            instance_id=row["instance_id"],
            plugin_id=row["plugin_id"],
            plugin_version=row["plugin_version"],
            status=ServiceStatus(row["status"]),
            database_path=row["database_path"],
        )

    def list_services(self, environment_id: str) -> tuple[ServiceInstance, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM environment_services
                 WHERE environment_id = ? ORDER BY instance_id
                """,
                (environment_id,),
            ).fetchall()
        return tuple(
            ServiceInstance(
                environment_id=row["environment_id"],
                instance_id=row["instance_id"],
                plugin_id=row["plugin_id"],
                plugin_version=row["plugin_version"],
                status=ServiceStatus(row["status"]),
                database_path=row["database_path"],
            )
            for row in rows
        )

    def list_mcp_surfaces(self, environment_id: str) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT surface_id FROM environment_mcp_surfaces
                 WHERE environment_id = ? ORDER BY surface_id
                """,
                (environment_id,),
            ).fetchall()
        return tuple(row["surface_id"] for row in rows)

    def get_template(self, template_id: str) -> StoredTemplate | None:
        with self._connect() as connection:
            template_row = connection.execute(
                "SELECT * FROM templates WHERE id = ?", (template_id,)
            ).fetchone()
            if template_row is None:
                return None
            service_rows = connection.execute(
                """
                SELECT * FROM template_services
                 WHERE template_id = ? ORDER BY instance_id
                """,
                (template_id,),
            ).fetchall()
            surface_rows = connection.execute(
                """
                SELECT surface_id FROM template_mcp_surfaces
                 WHERE template_id = ? ORDER BY surface_id
                """,
                (template_id,),
            ).fetchall()

        return StoredTemplate(
            template=self._to_template(template_row),
            services=tuple(
                TemplateService(
                    template_id=row["template_id"],
                    instance_id=row["instance_id"],
                    plugin_id=row["plugin_id"],
                    plugin_version=row["plugin_version"],
                    status=ServiceStatus(row["status"]),
                    database_path=row["database_path"],
                    seed=json.loads(row["config_json"]),
                )
                for row in service_rows
            ),
            mcp_surfaces=tuple(row["surface_id"] for row in surface_rows),
        )

    def list_templates(self) -> tuple[Template, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM templates ORDER BY created_at, id"
            ).fetchall()
        return tuple(self._to_template(row) for row in rows)

    def begin_operation(self, operation: OperationRecord) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO operations(
                    id, environment_id, service_instance_id, plugin_id, actor,
                    transport, operation_name, request_json, status, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation.id,
                    operation.environment_id,
                    operation.service_instance_id,
                    operation.plugin_id,
                    operation.actor,
                    operation.transport,
                    operation.operation,
                    json.dumps(operation.request, sort_keys=True, separators=(",", ":")),
                    operation.status,
                    operation.started_at.isoformat(),
                ),
            )

    def complete_operation(
        self,
        operation_id: str,
        status: OperationStatus,
        at: datetime,
        *,
        result: object | None = None,
        error: object | None = None,
    ) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE operations
                   SET status = ?, result_json = ?, error_json = ?, completed_at = ?
                 WHERE id = ? AND status = ?
                """,
                (
                    status,
                    None
                    if result is None
                    else json.dumps(result, sort_keys=True, separators=(",", ":")),
                    None
                    if error is None
                    else json.dumps(error, sort_keys=True, separators=(",", ":")),
                    at.isoformat(),
                    operation_id,
                    OperationStatus.RUNNING,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"operation {operation_id!r} is missing or already completed"
                )

    def list_operations(
        self,
        environment_id: str,
        *,
        service_instance_id: str | None = None,
        limit: int = 100,
    ) -> tuple[OperationRecord, ...]:
        statement = "SELECT * FROM operations WHERE environment_id = ?"
        parameters: list[object] = [environment_id]
        if service_instance_id is not None:
            statement += " AND service_instance_id = ?"
            parameters.append(service_instance_id)
        statement += " ORDER BY started_at, id LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(statement, tuple(parameters)).fetchall()
        return tuple(self._to_operation(row) for row in rows)

    def get_operation_cursor(self, environment_id: str) -> tuple[int, str | None]:
        with self._connect() as connection:
            count = connection.execute("SELECT count(*) FROM operations WHERE environment_id=?", (environment_id,)).fetchone()[0]
            row = connection.execute("SELECT id FROM operations WHERE environment_id=? ORDER BY started_at DESC,id DESC LIMIT 1", (environment_id,)).fetchone()
        return count, row[0] if row else None

    @staticmethod
    def _to_environment(row: sqlite3.Row) -> Environment:
        return Environment(
            id=row["id"],
            name=row["name"],
            status=EnvironmentStatus(row["status"]),
            template_id=row["template_id"],
            snapshot_id=row["snapshot_id"],
            failure_reason=row["failure_reason"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _to_template(row: sqlite3.Row) -> Template:
        return Template(
            id=row["id"],
            name=row["name"],
            version=row["version"],
            status=TemplateStatus(row["status"]),
            failure_reason=row["failure_reason"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _to_operation(row: sqlite3.Row) -> OperationRecord:
        return OperationRecord(
            id=row["id"],
            environment_id=row["environment_id"],
            service_instance_id=row["service_instance_id"],
            plugin_id=row["plugin_id"],
            actor=row["actor"],
            transport=row["transport"],
            operation=row["operation_name"],
            request=json.loads(row["request_json"]),
            status=OperationStatus(row["status"]),
            result=None if row["result_json"] is None else json.loads(row["result_json"]),
            error=None if row["error_json"] is None else json.loads(row["error_json"]),
            started_at=datetime.fromisoformat(row["started_at"]),
            completed_at=(
                None
                if row["completed_at"] is None
                else datetime.fromisoformat(row["completed_at"])
            ),
        )
