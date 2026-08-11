from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql

from mcp_system import MCPSystem, PluginRegistry
from mcp_system.config import load_template_spec
from mcp_system.http import GitHubHTTPRouter, MiddlewareHTTPServer
from mcp_system.http.base import FixedActorResolver
from mcp_system.service_plugins import GitHubPlugin
from mcp_system.storage import PostgresControlPlane, PostgresServiceStorage


CONFIG = Path("configs/templates/github-default.toml")
TOKEN = "local-test-token"


def registry() -> PluginRegistry:
    result = PluginRegistry()
    result.register(GitHubPlugin())
    return result


class RunningMiddleware:
    def __init__(self, router: GitHubHTTPRouter) -> None:
        self.server = MiddlewareHTTPServer(("127.0.0.1", 0), router)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "RunningMiddleware":
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def request(
        self,
        method: str,
        path: str,
        body: object | None = None,
        *,
        token: str | None = TOKEN,
        content_type: str = "application/json",
    ) -> tuple[int, dict[str, str], object | None]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Accept": "application/vnd.github+json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = content_type
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        response_headers = {key.casefold(): value for key, value in response.getheaders()}
        connection.close()
        return (
            response.status,
            response_headers,
            json.loads(raw) if raw else None,
        )


def exercise_http_contract(
    case: unittest.TestCase, system: MCPSystem, environment_id: str
) -> None:
    router = GitHubHTTPRouter(
        system,
        environment_id,
        actor_resolver=FixedActorResolver("engineer", token=TOKEN),
    )
    with RunningMiddleware(router) as middleware:
        status, _, error = middleware.request("GET", "/user", token=None)
        case.assertEqual(status, 401)
        case.assertEqual(error["message"], "Bad credentials")

        status, headers, actor = middleware.request("GET", "/api/v3/user")
        case.assertEqual(status, 200)
        case.assertEqual(actor["login"], "engineer")
        case.assertEqual(headers["x-github-api-version-selected"], "2026-03-10")

        status, _, issue = middleware.request(
            "POST",
            "/repos/acme/product/issues",
            {
                "title": "Intercepted agent request",
                "body": "This never reached github.com",
                "labels": ["bug"],
                "assignees": ["qa"],
            },
        )
        case.assertEqual(status, 201)
        case.assertEqual(issue["number"], 1)
        case.assertEqual(issue["user"]["login"], "engineer")

        status, _, issues = middleware.request(
            "GET", "/repos/acme/product/issues?state=open"
        )
        case.assertEqual(status, 200)
        case.assertEqual([item["number"] for item in issues], [1])

        status, _, invalid = middleware.request(
            "POST", "/repos/acme/product/issues", {"body": "missing title"}
        )
        case.assertEqual(status, 422)
        case.assertIn("title", invalid["message"])

        status, _, actions = middleware.request(
            "GET", "/repos/acme/product/actions/runs"
        )
        case.assertEqual(status, 200)
        case.assertEqual(actions, {"total_count": 0, "workflow_runs": []})

        status, _, forbidden = middleware.request(
            "PATCH", "/repos/acme/product", {"archived": True}
        )
        case.assertEqual(status, 403)
        case.assertEqual(forbidden["message"], "Resource not accessible by integration")

    with system.open_service_database(environment_id, "github") as session:
        persisted = session.execute(
            "SELECT title, comments_count FROM github_issues WHERE number = 1"
        ).fetchone()
        case.assertEqual(persisted["title"], "Intercepted agent request")
        case.assertEqual(persisted["comments_count"], 0)


class HTTPMiddlewareSQLiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.system = MCPSystem(Path(self.temp_dir.name), registry())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_real_http_interception_and_persistence(self) -> None:
        template = self.system.create_template(load_template_spec(CONFIG))
        environment = self.system.create_environment_from_template(template.id)
        exercise_http_contract(self, self.system, environment.id)


@unittest.skipUnless(
    os.getenv("MCP_SYSTEM_TEST_POSTGRES_DSN"),
    "set MCP_SYSTEM_TEST_POSTGRES_DSN to run PostgreSQL integration tests",
)
class HTTPMiddlewarePostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dsn = os.environ["MCP_SYSTEM_TEST_POSTGRES_DSN"]
        suffix = uuid4().hex[:10]
        self.control_schema = f"http_control_{suffix}"
        self.storage_namespace = f"http_state_{suffix}"
        self.temp_dir = tempfile.TemporaryDirectory()
        self.system = MCPSystem(
            Path(self.temp_dir.name),
            registry(),
            control_plane=PostgresControlPlane(self.dsn, schema=self.control_schema),
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

    def test_real_http_interception_and_persistence(self) -> None:
        template = self.system.create_template(load_template_spec(CONFIG))
        environment = self.system.create_environment_from_template(template.id)
        exercise_http_contract(self, self.system, environment.id)


if __name__ == "__main__":
    unittest.main()
