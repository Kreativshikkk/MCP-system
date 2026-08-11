"""PostgreSQL schema-per-service persistence backend."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import re
from typing import Any, Iterator, Mapping, Sequence

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from ..errors import ConfigurationError
from ..plugins import PluginRegistry, RelationalResult, RelationalSession, ServicePlugin


_PG_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class PostgresSession(RelationalSession):
    """Neutral qmark-parameter session backed by a psycopg connection."""

    def __init__(self, connection: psycopg.Connection[dict[str, Any]]) -> None:
        self.connection = connection

    def execute(
        self, statement: str, parameters: Sequence[Any] = ()
    ) -> RelationalResult:
        return self.connection.execute(
            self._adapt_parameters(statement, parameters), parameters
        )

    def executemany(
        self, statement: str, parameters: Sequence[Sequence[Any]]
    ) -> RelationalResult:
        adapted = statement.replace("?", "%s")
        cursor = self.connection.cursor()
        cursor.executemany(adapted, parameters)
        return cursor

    @staticmethod
    def _adapt_parameters(statement: str, parameters: Sequence[Any]) -> str:
        if not parameters:
            return statement
        if statement.count("?") != len(parameters):
            raise ConfigurationError(
                "relational statements must use one '?' placeholder per parameter"
            )
        return statement.replace("?", "%s")


class PostgresServiceStorage:
    """Stores every service instance in its own PostgreSQL schema."""

    kind = "postgresql"

    def __init__(self, dsn: str, *, namespace: str = "mcp") -> None:
        if not _PG_IDENTIFIER.fullmatch(namespace):
            raise ConfigurationError("invalid PostgreSQL storage namespace")
        self.dsn = dsn
        self.namespace = namespace

    def build_locator(self, environment_id: str, instance_id: str) -> str:
        return self._locator("env", environment_id, instance_id)

    def build_template_locator(self, template_id: str, instance_id: str) -> str:
        return self._locator("tpl", template_id, instance_id)

    def build_snapshot_locator(self, snapshot_id: str, instance_id: str) -> str:
        return self._locator("snap", snapshot_id, instance_id)

    def provision(
        self,
        locator: str,
        plugin: ServicePlugin,
        seed: Mapping[str, Any],
    ) -> None:
        self._validate_locator(locator)
        migrations = PluginRegistry.validate_migrations(plugin, self.kind)
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            self._create_empty_schema(connection, locator, migrations)
            self._set_search_path(connection, locator)
            plugin.seed(PostgresSession(connection), seed)

    @contextmanager
    def open(self, locator: str) -> Iterator[RelationalSession]:
        self._validate_locator(locator)
        connection = psycopg.connect(self.dsn, row_factory=dict_row)
        try:
            self._require_schema(connection, locator)
            self._set_search_path(connection, locator)
            yield PostgresSession(connection)
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
        self._validate_locator(source_locator)
        self._validate_locator(target_locator)
        migrations = PluginRegistry.validate_migrations(plugin, self.kind)

        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            self._require_schema(connection, source_locator)
            self._create_empty_schema(connection, target_locator, migrations)
            connection.execute("SET CONSTRAINTS ALL DEFERRED")

            tables = self._ordered_tables(connection, source_locator)
            for table in tables:
                connection.execute(
                    sql.SQL("INSERT INTO {}.{} SELECT * FROM {}.{}").format(
                        sql.Identifier(target_locator),
                        sql.Identifier(table),
                        sql.Identifier(source_locator),
                        sql.Identifier(table),
                    )
                )
            self._reset_sequences(connection, target_locator, tables)

    def inspect(self, locator: str) -> Mapping[str, Any]:
        self._validate_locator(locator)
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            self._require_schema(connection, locator)
            tables = [row["table_name"] for row in connection.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_type='BASE TABLE' AND table_name <> '_mcp_plugin_migrations' ORDER BY table_name", (locator,)).fetchall()]
            result: dict[str, Any] = {}
            for table in tables:
                columns = [row["column_name"] for row in connection.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position", (locator, table)).fetchall()]
                primary_key = [row["column_name"] for row in connection.execute("""SELECT a.attname AS column_name FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid JOIN pg_namespace n ON n.oid=c.relnamespace JOIN unnest(i.indkey) WITH ORDINALITY AS key(attnum,ord) ON true JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=key.attnum WHERE n.nspname=%s AND c.relname=%s AND i.indisprimary ORDER BY key.ord""", (locator, table)).fetchall()]
                rows = connection.execute(sql.SQL("SELECT * FROM {}.{}").format(sql.Identifier(locator), sql.Identifier(table))).fetchall()
                result[table] = {"columns": columns, "primaryKey": primary_key, "rows": [dict(row) for row in rows]}
            return result

    def _create_empty_schema(
        self,
        connection: psycopg.Connection[dict[str, Any]],
        locator: str,
        migrations: Sequence[Any],
    ) -> None:
        exists = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname = %s)",
            (locator,),
        ).fetchone()["exists"]
        if exists:
            raise ConfigurationError(
                f"PostgreSQL service schema already exists: {locator}"
            )

        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(locator)))
        self._set_search_path(connection, locator)
        connection.execute(
            """
            CREATE TABLE _mcp_plugin_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        for migration in migrations:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO _mcp_plugin_migrations(version) VALUES (%s)",
                (migration.version,),
            )

    @staticmethod
    def _set_search_path(
        connection: psycopg.Connection[dict[str, Any]], locator: str
    ) -> None:
        connection.execute(
            sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(locator))
        )

    @staticmethod
    def _require_schema(
        connection: psycopg.Connection[dict[str, Any]], locator: str
    ) -> None:
        exists = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname = %s)",
            (locator,),
        ).fetchone()["exists"]
        if not exists:
            raise ConfigurationError(
                f"PostgreSQL service schema does not exist: {locator}"
            )

    @staticmethod
    def _ordered_tables(
        connection: psycopg.Connection[dict[str, Any]], schema: str
    ) -> list[str]:
        rows = connection.execute(
            """
            SELECT table_name
              FROM information_schema.tables
             WHERE table_schema = %s AND table_type = 'BASE TABLE'
               AND table_name <> '_mcp_plugin_migrations'
             ORDER BY table_name
            """,
            (schema,),
        ).fetchall()
        tables = {row["table_name"] for row in rows}
        dependencies: dict[str, set[str]] = {table: set() for table in tables}
        foreign_keys = connection.execute(
            """
            SELECT child.relname AS child_table, parent.relname AS parent_table
              FROM pg_constraint constraint_row
              JOIN pg_class child ON child.oid = constraint_row.conrelid
              JOIN pg_namespace child_ns ON child_ns.oid = child.relnamespace
              JOIN pg_class parent ON parent.oid = constraint_row.confrelid
             WHERE constraint_row.contype = 'f' AND child_ns.nspname = %s
            """,
            (schema,),
        ).fetchall()
        for row in foreign_keys:
            child = row["child_table"]
            parent = row["parent_table"]
            if child != parent and child in dependencies and parent in tables:
                dependencies[child].add(parent)

        ordered: list[str] = []
        remaining = set(tables)
        while remaining:
            ready = sorted(
                table for table in remaining if not (dependencies[table] & remaining)
            )
            if not ready:
                # Cyclic foreign keys must be declared DEFERRABLE by the plugin.
                ordered.extend(sorted(remaining))
                break
            ordered.extend(ready)
            remaining.difference_update(ready)
        return ordered

    @staticmethod
    def _reset_sequences(
        connection: psycopg.Connection[dict[str, Any]],
        schema: str,
        tables: Sequence[str],
    ) -> None:
        for table in tables:
            columns = connection.execute(
                """
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_schema = %s AND table_name = %s
                   AND (column_default LIKE 'nextval(%%' OR is_identity = 'YES')
                """,
                (schema, table),
            ).fetchall()
            for row in columns:
                column = row["column_name"]
                relation = f'"{schema}"."{table}"'
                sequence_row = connection.execute(
                    "SELECT pg_get_serial_sequence(%s, %s) AS sequence_name",
                    (relation, column),
                ).fetchone()
                sequence_name = sequence_row["sequence_name"]
                if not sequence_name:
                    continue
                maximum = connection.execute(
                    sql.SQL("SELECT max({}) AS maximum FROM {}.{}").format(
                        sql.Identifier(column),
                        sql.Identifier(schema),
                        sql.Identifier(table),
                    )
                ).fetchone()["maximum"]
                if maximum is None:
                    connection.execute("SELECT setval(%s, 1, false)", (sequence_name,))
                else:
                    connection.execute(
                        "SELECT setval(%s, %s, true)", (sequence_name, maximum)
                    )

    def _locator(self, scope: str, owner_id: str, instance_id: str) -> str:
        readable = f"{self.namespace}_{scope}_{owner_id}_{instance_id}".replace(
            "-", "_"
        )
        if len(readable) <= 63 and _PG_IDENTIFIER.fullmatch(readable):
            return readable
        digest = hashlib.sha256(readable.encode("utf-8")).hexdigest()[:12]
        prefix = readable[: 63 - len(digest) - 1]
        locator = f"{prefix}_{digest}"
        self._validate_locator(locator)
        return locator

    def _validate_locator(self, locator: str) -> None:
        if not _PG_IDENTIFIER.fullmatch(locator) or not locator.startswith(
            f"{self.namespace}_"
        ):
            raise ConfigurationError(f"invalid PostgreSQL service locator: {locator}")
