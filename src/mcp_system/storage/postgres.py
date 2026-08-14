"""PostgreSQL implementation of the persistent MCPSystem control plane."""

from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from ..errors import ConfigurationError
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


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

_MIGRATION_1 = """
CREATE TABLE templates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL,
    failure_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE(name, version)
);

CREATE TABLE template_services (
    template_id TEXT NOT NULL REFERENCES templates(id),
    instance_id TEXT NOT NULL,
    plugin_id TEXT NOT NULL,
    plugin_version TEXT NOT NULL,
    status TEXT NOT NULL,
    database_path TEXT NOT NULL,
    config_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (template_id, instance_id)
);

CREATE TABLE template_mcp_surfaces (
    template_id TEXT NOT NULL REFERENCES templates(id),
    surface_id TEXT NOT NULL,
    PRIMARY KEY (template_id, surface_id)
);

CREATE TABLE environments (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    template_id TEXT REFERENCES templates(id),
    failure_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE environment_services (
    environment_id TEXT NOT NULL REFERENCES environments(id),
    instance_id TEXT NOT NULL,
    plugin_id TEXT NOT NULL,
    plugin_version TEXT NOT NULL,
    status TEXT NOT NULL,
    database_path TEXT NOT NULL,
    config_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (environment_id, instance_id)
);

CREATE TABLE environment_mcp_surfaces (
    environment_id TEXT NOT NULL REFERENCES environments(id),
    surface_id TEXT NOT NULL,
    PRIMARY KEY (environment_id, surface_id)
);
"""

_MIGRATION_2 = """
CREATE TABLE operations (
    id TEXT PRIMARY KEY,
    environment_id TEXT NOT NULL,
    service_instance_id TEXT NOT NULL,
    plugin_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    transport TEXT NOT NULL,
    operation_name TEXT NOT NULL,
    request_json JSONB NOT NULL,
    status TEXT NOT NULL,
    result_json JSONB,
    error_json JSONB,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    FOREIGN KEY (environment_id, service_instance_id)
        REFERENCES environment_services(environment_id, instance_id)
);

CREATE INDEX operations_environment_timeline
    ON operations(environment_id, started_at, id);
"""

_MIGRATION_3 = """
ALTER TABLE environments ADD COLUMN snapshot_id TEXT;
"""

# 4: a real watermark (monotonic per-environment seq), the MCP request id the
# operation came from, and the cross-process freeze flag.
_MIGRATION_4 = """
ALTER TABLE environments ADD COLUMN frozen BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE operations ADD COLUMN seq BIGINT NOT NULL DEFAULT 0;
ALTER TABLE operations ADD COLUMN tool_call_id TEXT;
CREATE INDEX operations_environment_seq ON operations(environment_id, seq);
"""


class PostgresControlPlane:
    def __init__(self, dsn: str, *, schema: str = "mcp_control") -> None:
        if not _IDENTIFIER.fullmatch(schema):
            raise ConfigurationError("invalid PostgreSQL control-plane schema")
        self.dsn = dsn
        self.schema = schema

    def _connect(self) -> psycopg.Connection[dict[str, Any]]:
        connection = psycopg.connect(self.dsn, row_factory=dict_row)
        connection.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema))
        )
        return connection

    def initialize(self) -> None:
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            connection.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                    sql.Identifier(self.schema)
                )
            )
            connection.execute(
                sql.SQL("SET LOCAL search_path TO {}").format(
                    sql.Identifier(self.schema)
                )
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS control_plane_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            applied = {
                row["version"]
                for row in connection.execute(
                    "SELECT version FROM control_plane_migrations"
                ).fetchall()
            }
            if 1 not in applied:
                for statement in _split_statements(_MIGRATION_1):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO control_plane_migrations(version) VALUES (1)"
                )
            if 2 not in applied:
                for statement in _split_statements(_MIGRATION_2):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO control_plane_migrations(version) VALUES (2)"
                )
            if 3 not in applied:
                for statement in _split_statements(_MIGRATION_3):
                    connection.execute(statement)
                connection.execute("INSERT INTO control_plane_migrations(version) VALUES (3)")
            if 4 not in applied:
                for statement in _split_statements(_MIGRATION_4):
                    connection.execute(statement)
                connection.execute("INSERT INTO control_plane_migrations(version) VALUES (4)")

    def recover_interrupted_provisioning(self, at: datetime) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE environment_services SET status = %s, updated_at = %s
                 WHERE status = %s
                """,
                (ServiceStatus.FAILED, at, ServiceStatus.PROVISIONING),
            )
            connection.execute(
                """
                UPDATE environments
                   SET status = %s, failure_reason = %s, updated_at = %s
                 WHERE status = %s
                """,
                (
                    EnvironmentStatus.FAILED,
                    "provisioning was interrupted by a previous process",
                    at,
                    EnvironmentStatus.PROVISIONING,
                ),
            )
            connection.execute(
                """
                UPDATE template_services SET status = %s, updated_at = %s
                 WHERE status = %s
                """,
                (ServiceStatus.FAILED, at, ServiceStatus.PROVISIONING),
            )
            connection.execute(
                """
                UPDATE templates
                   SET status = %s, failure_reason = %s, updated_at = %s
                 WHERE status = %s
                """,
                (
                    TemplateStatus.FAILED,
                    "provisioning was interrupted by a previous process",
                    at,
                    TemplateStatus.PROVISIONING,
                ),
            )
            connection.execute(
                """
                UPDATE operations
                   SET status = %s, error_json = %s, completed_at = %s
                 WHERE status = %s
                """,
                (
                    OperationStatus.INTERRUPTED,
                    json.dumps(
                        {
                            "type": "interrupted",
                            "message": "operation was interrupted by a previous process",
                        }
                    ),
                    at,
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
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO environments(
                    id, name, status, template_id, snapshot_id, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    environment_id,
                    spec.name,
                    EnvironmentStatus.PROVISIONING,
                    template_id,
                    snapshot_id,
                    at,
                    at,
                ),
            )
            with connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO environment_services(
                        environment_id, instance_id, plugin_id, plugin_version,
                        status, database_path, config_json, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            environment_id,
                            service.instance_id,
                            service.plugin_id,
                            service.plugin_version,
                            ServiceStatus.PROVISIONING,
                            service_paths[service.instance_id],
                            json.dumps(service.seed),
                            at,
                            at,
                        )
                        for service in spec.services
                    ],
                )
                cursor.executemany(
                    """
                    INSERT INTO environment_mcp_surfaces(environment_id, surface_id)
                    VALUES (%s, %s)
                    """,
                    [(environment_id, surface) for surface in spec.mcp_surfaces],
                )

    def create_template_record(
        self,
        spec: TemplateSpec,
        service_paths: dict[str, str],
        at: datetime,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO templates(
                    id, name, version, status, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    spec.template_id,
                    spec.name,
                    spec.version,
                    TemplateStatus.PROVISIONING,
                    at,
                    at,
                ),
            )
            with connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO template_services(
                        template_id, instance_id, plugin_id, plugin_version,
                        status, database_path, config_json, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            spec.template_id,
                            service.instance_id,
                            service.plugin_id,
                            service.plugin_version,
                            ServiceStatus.PROVISIONING,
                            service_paths[service.instance_id],
                            json.dumps(service.seed),
                            at,
                            at,
                        )
                        for service in spec.services
                    ],
                )
                cursor.executemany(
                    """
                    INSERT INTO template_mcp_surfaces(template_id, surface_id)
                    VALUES (%s, %s)
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
                   SET status = %s, failure_reason = %s, updated_at = %s
                 WHERE id = %s
                """,
                (status, failure_reason, at, environment_id),
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
                UPDATE environment_services SET status = %s, updated_at = %s
                 WHERE environment_id = %s AND instance_id = %s
                """,
                (status, at, environment_id, instance_id),
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
                   SET status = %s, failure_reason = %s, updated_at = %s
                 WHERE id = %s
                """,
                (status, failure_reason, at, template_id),
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
                UPDATE template_services SET status = %s, updated_at = %s
                 WHERE template_id = %s AND instance_id = %s
                """,
                (status, at, template_id, instance_id),
            )

    def get_environment(self, environment_id: str) -> Environment | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM environments WHERE id = %s", (environment_id,)
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
                 WHERE environment_id = %s AND instance_id = %s
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
                 WHERE environment_id = %s ORDER BY instance_id
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
                 WHERE environment_id = %s ORDER BY surface_id
                """,
                (environment_id,),
            ).fetchall()
        return tuple(row["surface_id"] for row in rows)

    def get_template(self, template_id: str) -> StoredTemplate | None:
        with self._connect() as connection:
            template_row = connection.execute(
                "SELECT * FROM templates WHERE id = %s", (template_id,)
            ).fetchone()
            if template_row is None:
                return None
            service_rows = connection.execute(
                """
                SELECT * FROM template_services
                 WHERE template_id = %s ORDER BY instance_id
                """,
                (template_id,),
            ).fetchall()
            surface_rows = connection.execute(
                """
                SELECT surface_id FROM template_mcp_surfaces
                 WHERE template_id = %s ORDER BY surface_id
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
                    seed=row["config_json"],
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

    def begin_operation(self, operation: OperationRecord) -> int:
        """Insert the record and return its monotonic per-environment seq."""
        with self._connect() as connection:
            # lock the environment row so concurrent writers cannot mint the
            # same seq; the watermark is only useful if it is really monotonic
            connection.execute(
                "SELECT id FROM environments WHERE id = %s FOR UPDATE",
                (operation.environment_id,),
            )
            row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) AS seq FROM operations"
                " WHERE environment_id = %s",
                (operation.environment_id,),
            ).fetchone()
            seq = int(row["seq"]) + 1
            connection.execute(
                """
                INSERT INTO operations(
                    id, environment_id, service_instance_id, plugin_id, actor,
                    transport, operation_name, request_json, status, started_at,
                    seq, tool_call_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    operation.id,
                    operation.environment_id,
                    operation.service_instance_id,
                    operation.plugin_id,
                    operation.actor,
                    operation.transport,
                    operation.operation,
                    json.dumps(operation.request),
                    operation.status,
                    operation.started_at,
                    seq,
                    operation.tool_call_id,
                ),
            )
            return seq

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
                   SET status = %s, result_json = %s, error_json = %s,
                       completed_at = %s
                 WHERE id = %s AND status = %s
                """,
                (
                    status,
                    None if result is None else json.dumps(result),
                    None if error is None else json.dumps(error),
                    at,
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
        since_seq: int = 0,
        actor: str | None = None,
    ) -> tuple[OperationRecord, ...]:
        statement = "SELECT * FROM operations WHERE environment_id = %s AND seq > %s"
        parameters: list[object] = [environment_id, since_seq]
        if service_instance_id is not None:
            statement += " AND service_instance_id = %s"
            parameters.append(service_instance_id)
        if actor is not None:
            statement += " AND actor = %s"
            parameters.append(actor)
        statement += " ORDER BY seq LIMIT %s"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(statement, tuple(parameters)).fetchall()
        return tuple(self._to_operation(row) for row in rows)

    def operation_watermark(self, environment_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) AS seq FROM operations"
                " WHERE environment_id = %s",
                (environment_id,),
            ).fetchone()
        return int(row["seq"])

    def get_operation_cursor(self, environment_id: str) -> tuple[int, str | None]:
        with self._connect() as connection:
            count = connection.execute("SELECT count(*) AS count FROM operations WHERE environment_id=%s", (environment_id,)).fetchone()["count"]
            row = connection.execute("SELECT id FROM operations WHERE environment_id=%s ORDER BY seq DESC LIMIT 1", (environment_id,)).fetchone()
        return count, row["id"] if row else None

    def set_environment_frozen(
        self, environment_id: str, frozen: bool, at: datetime
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE environments SET frozen = %s, updated_at = %s WHERE id = %s",
                (frozen, at, environment_id),
            )

    def delete_environment_record(self, environment_id: str) -> None:
        """Remove the environment and everything that references it."""
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM operations WHERE environment_id = %s", (environment_id,)
            )
            connection.execute(
                "DELETE FROM environment_mcp_surfaces WHERE environment_id = %s",
                (environment_id,),
            )
            connection.execute(
                "DELETE FROM environment_services WHERE environment_id = %s",
                (environment_id,),
            )
            connection.execute(
                "DELETE FROM environments WHERE id = %s", (environment_id,)
            )

    @staticmethod
    def _to_environment(row: dict[str, Any]) -> Environment:
        return Environment(
            id=row["id"],
            name=row["name"],
            status=EnvironmentStatus(row["status"]),
            template_id=row["template_id"],
            snapshot_id=row["snapshot_id"],
            failure_reason=row["failure_reason"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            frozen=bool(row["frozen"]),
        )

    @staticmethod
    def _to_template(row: dict[str, Any]) -> Template:
        return Template(
            id=row["id"],
            name=row["name"],
            version=row["version"],
            status=TemplateStatus(row["status"]),
            failure_reason=row["failure_reason"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _to_operation(row: dict[str, Any]) -> OperationRecord:
        return OperationRecord(
            id=row["id"],
            environment_id=row["environment_id"],
            service_instance_id=row["service_instance_id"],
            plugin_id=row["plugin_id"],
            actor=row["actor"],
            transport=row["transport"],
            operation=row["operation_name"],
            request=row["request_json"],
            status=OperationStatus(row["status"]),
            result=row["result_json"],
            error=row["error_json"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            seq=int(row["seq"]),
            tool_call_id=row["tool_call_id"],
        )


def _split_statements(script: str) -> tuple[str, ...]:
    """Split the simple built-in DDL migration into executable statements."""
    return tuple(statement.strip() for statement in script.split(";") if statement.strip())
