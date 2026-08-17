from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql

from mcp_system import MCPSystem, PluginRegistry
from mcp_system.config import load_template_spec
from mcp_system.mcp import MCPDispatcher, MCPJSONRPCServer
from mcp_system.service_plugins import GitHubPlugin
from mcp_system.storage import PostgresControlPlane, PostgresServiceStorage


CONFIG = Path("configs/templates/github-default.toml")


def registry() -> PluginRegistry:
    result = PluginRegistry()
    result.register(GitHubPlugin())
    return result


def ready_server(system: MCPSystem, environment_id: str) -> MCPJSONRPCServer:
    dispatcher = MCPDispatcher(
        system,
        environment_id,
        actor="engineer",
        bindings={"github_rest_v3": "github"},
    )
    server = MCPJSONRPCServer(dispatcher)
    initialized = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0"},
            },
        }
    )
    assert initialized is not None
    assert initialized["result"]["protocolVersion"] == "2025-11-25"
    assert server.handle(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    ) is None
    return server


def call(
    server: MCPJSONRPCServer,
    request_id: int,
    name: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert response is not None
    return response


def exercise_mcp_contract(
    case: unittest.TestCase, system: MCPSystem, environment_id: str
) -> None:
    server = ready_server(system, environment_id)
    listed = server.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    )
    case.assertIsNotNone(listed)
    tools = listed["result"]["tools"]
    case.assertEqual(len(tools), 51)
    case.assertLessEqual(
        {"github_create_issue", "github_list_workflow_runs", "github_list_workflow_jobs", "github_get_workflow_job", "github_get_workflow_job_log", "github_list_releases", "github_create_release"},
        {tool["name"] for tool in tools},
    )
    case.assertTrue(
        next(tool for tool in tools if tool["name"] == "github_get_issue")[
            "annotations"
        ]["readOnlyHint"]
    )

    actor = call(server, 3, "github_get_authenticated_user", {})
    case.assertEqual(
        actor["result"]["structuredContent"]["result"]["login"], "engineer"
    )
    created_issue = call(
        server,
        4,
        "github_create_issue",
        {"owner": "acme", "repo": "product", "title": "Created through MCP"},
    )
    case.assertFalse(created_issue["result"]["isError"])
    case.assertEqual(
        created_issue["result"]["structuredContent"]["result"]["number"], 1
    )

    invalid = call(
        server,
        5,
        "github_create_issue",
        {
            "owner": "acme",
            "repo": "product",
            "title": "Invalid",
            "unexpected": True,
        },
    )
    case.assertTrue(invalid["result"]["isError"])
    case.assertEqual(
        invalid["result"]["structuredContent"]["error"]["type"],
        "invalid_arguments",
    )

    commit = call(
        server,
        6,
        "github_create_commit",
        {
            "owner": "acme",
            "repo": "product",
            "message": "Initial MCP commit",
            "author": "engineer",
            "files": {"README.md": "# Created over MCP\n"},
        },
    )
    sha = commit["result"]["structuredContent"]["result"]["sha"]
    branch = call(
        server,
        7,
        "github_create_branch",
        {
            "owner": "acme",
            "repo": "product",
            "name": "main",
            "head_sha": sha,
        },
    )
    case.assertFalse(branch["result"]["isError"])
    repository = system.open_git_data_plane(environment_id, "github").repository(1)
    case.assertEqual(repository.object_type(sha), "commit")
    case.assertEqual(repository.resolve_branch("main"), sha)

    unknown = call(server, 8, "github_does_not_exist", {})
    case.assertEqual(unknown["error"]["code"], -32602)

    with system.open_service_database(environment_id, "github") as session:
        case.assertEqual(
            session.execute("SELECT count(*) AS count FROM github_issues").fetchone()[
                "count"
            ],
            1,
        )

    operation_log = system.list_operations(environment_id)
    case.assertEqual(
        [entry.operation for entry in operation_log],
        [
            "get_authenticated_user",
            "create_issue",
            "create_commit",
            "create_branch",
        ],
    )
    case.assertTrue(all(entry.status == "succeeded" for entry in operation_log))
    case.assertTrue(all(entry.transport == "mcp" for entry in operation_log))


class MCPSQLiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temp_dir.name)
        self.system = MCPSystem(self.data_root, registry())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_protocol_dispatch_and_persistence(self) -> None:
        template = self.system.create_template(load_template_spec(CONFIG))
        environment = self.system.create_environment_from_template(template.id)
        exercise_mcp_contract(self, self.system, environment.id)

    def test_lifecycle_and_previous_protocol_revision(self) -> None:
        template = self.system.create_template(load_template_spec(CONFIG))
        environment = self.system.create_environment_from_template(template.id)
        server = MCPJSONRPCServer(
            MCPDispatcher(
                self.system,
                environment.id,
                actor="qa",
                bindings={"github_rest_v3": "github"},
            )
        )
        premature = server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        self.assertEqual(premature["error"]["code"], -32002)
        initialized = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "older-client", "version": "1"},
                },
            }
        )
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")

    def test_real_stdio_subprocess(self) -> None:
        template = self.system.create_template(load_template_spec(CONFIG))
        environment = self.system.create_environment_from_template(template.id)
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "stdio-test", "version": "1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "github_create_issue",
                    "arguments": {
                        "owner": "acme",
                        "repo": "product",
                        "title": "stdio issue",
                    },
                },
            },
        ]
        process = subprocess.run(
            (
                sys.executable,
                "scripts/mcp_server.py",
                "--data-root",
                str(self.data_root),
                "--environment",
                environment.id,
                "--actor",
                "engineer",
            ),
            input="".join(json.dumps(message) + "\n" for message in messages),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=15,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([response["id"] for response in responses], [1, 2, 3])
        self.assertEqual(
            responses[2]["result"]["structuredContent"]["result"]["number"], 1
        )


@unittest.skipUnless(
    os.getenv("MCP_SYSTEM_TEST_POSTGRES_DSN"),
    "set MCP_SYSTEM_TEST_POSTGRES_DSN to run PostgreSQL integration tests",
)
class MCPPostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dsn = os.environ["MCP_SYSTEM_TEST_POSTGRES_DSN"]
        suffix = uuid4().hex[:10]
        self.control_schema = f"mcp_rpc_control_{suffix}"
        self.storage_namespace = f"mcp_rpc_state_{suffix}"
        self.temp_dir = tempfile.TemporaryDirectory()
        self.system = MCPSystem(
            Path(self.temp_dir.name),
            registry(),
            control_plane=PostgresControlPlane(
                self.dsn, schema=self.control_schema
            ),
            service_storage=PostgresServiceStorage(
                self.dsn, namespace=self.storage_namespace
            ),
        )

    def tearDown(self) -> None:
        with psycopg.connect(self.dsn) as connection:
            rows = connection.execute(
                """
                SELECT nspname FROM pg_namespace
                 WHERE nspname = %s OR nspname LIKE %s
                """,
                (self.control_schema, f"{self.storage_namespace}\\_%"),
            ).fetchall()
            for (schema_name,) in rows:
                connection.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(
                        sql.Identifier(schema_name)
                    )
                )
        self.temp_dir.cleanup()

    def test_protocol_dispatch_and_persistence(self) -> None:
        template = self.system.create_template(load_template_spec(CONFIG))
        environment = self.system.create_environment_from_template(template.id)
        exercise_mcp_contract(self, self.system, environment.id)


if __name__ == "__main__":
    unittest.main()
