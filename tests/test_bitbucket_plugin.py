from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql

from mcp_system import MCPSystem, PluginRegistry
from mcp_system.config import load_template_spec
from mcp_system.http import HTTPRequest, InspectorHTTPRouter
from mcp_system.mcp import MCPDispatcher
from mcp_system.service_plugins import BitbucketPlugin
from mcp_system.storage import PostgresControlPlane, PostgresServiceStorage


CONFIG = Path("configs/templates/bitbucket-default.toml")


class BitbucketPluginTests(unittest.TestCase):
    def test_permissions_conflicts_and_validation_are_correctable_tool_errors(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            registry = PluginRegistry(); registry.register(BitbucketPlugin())
            system = MCPSystem(Path(root), registry); spec = load_template_spec(CONFIG)
            system.create_template(spec); environment = system.create_environment_from_template(spec.template_id)
            engineer = MCPDispatcher(system, environment.id, actor="engineer")
            base = engineer.call_tool("bitbucket_create_commit", {"workspace":"acme","repo_slug":"product","branch":"main","message":"Initial","files":{"a.txt":"a\n"}})
            self.assertFalse(base["isError"])
            missing_file = engineer.call_tool(
                "bitbucket_get_file",
                {
                    "workspace": "acme",
                    "repo_slug": "product",
                    "commit": "main",
                    "path": "missing.txt",
                },
            )
            forbidden = engineer.call_tool("bitbucket_delete_branch", {"workspace":"acme","repo_slug":"product","name":"main"})
            invalid = engineer.call_tool("bitbucket_update_issue", {"workspace":"acme","repo_slug":"product","issue_id":999,"state":"impossible"})
            unknown = MCPDispatcher(system, environment.id, actor="intruder").call_tool("bitbucket_get_current_user", {})
            self.assertEqual(forbidden["structuredContent"]["error"]["status"], 403)
            self.assertEqual(missing_file["structuredContent"]["error"]["status"], 404)
            self.assertEqual(missing_file["structuredContent"]["error"]["type"], "not_found")
            self.assertEqual(invalid["structuredContent"]["error"]["status"], 404)
            self.assertEqual(unknown["structuredContent"]["error"]["status"], 403)
            self.assertEqual([item.status for item in system.list_operations(environment.id)][-4:], ["failed", "failed", "failed", "failed"])

    def test_ticket_commit_review_pipeline_merge_and_inspector(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            registry = PluginRegistry(); registry.register(BitbucketPlugin())
            system = MCPSystem(Path(root), registry)
            spec = load_template_spec(CONFIG); system.create_template(spec)
            environment = system.create_environment_from_template(spec.template_id)
            engineer = MCPDispatcher(system, environment.id, actor="engineer")
            qa = MCPDispatcher(system, environment.id, actor="qa")
            lead = MCPDispatcher(system, environment.id, actor="lead")
            self.assertEqual(len(engineer.list_tools()), 36)

            def call(dispatcher: MCPDispatcher, name: str, arguments: dict[str, object]) -> object:
                response = dispatcher.call_tool(name, arguments)
                self.assertFalse(response["isError"], response["structuredContent"])
                return response["structuredContent"]["result"]

            issue = call(engineer, "bitbucket_create_issue", {"workspace": "acme", "repo_slug": "product", "title": "Refresh race", "content": "Refresh can lose state", "assignee": "engineer"})
            base = call(engineer, "bitbucket_create_commit", {"workspace": "acme", "repo_slug": "product", "branch": "main", "message": "Initial commit", "files": {"app.py": "SAFE = False\n"}})
            head = call(engineer, "bitbucket_create_commit", {"workspace": "acme", "repo_slug": "product", "branch": "fix/refresh", "message": f"Issue #{issue['id']} fix refresh", "parents": [base["hash"]], "files": {"app.py": "SAFE = True\n"}})
            pull = call(engineer, "bitbucket_create_pull_request", {"workspace": "acme", "repo_slug": "product", "title": "Fix refresh race", "source_branch": "fix/refresh", "destination_branch": "main", "reviewers": ["qa"]})
            diff = call(qa, "bitbucket_get_pull_request_diff", {"workspace": "acme", "repo_slug": "product", "pull_request_id": pull["id"]})
            self.assertIn("+SAFE = True", diff["patch"])
            call(qa, "bitbucket_create_pull_request_comment", {"workspace": "acme", "repo_slug": "product", "pull_request_id": pull["id"], "content": "Verified the refresh path."})
            call(qa, "bitbucket_approve_pull_request", {"workspace": "acme", "repo_slug": "product", "pull_request_id": pull["id"]})
            pipeline = call(engineer, "bitbucket_create_pipeline", {"workspace": "acme", "repo_slug": "product", "ref_name": "fix/refresh"})
            system.invoke_service_operation(environment.id, "bitbucket", actor="director", transport="ci-harness", operation="update_pipeline", arguments={"workspace": "acme", "repo_slug": "product", "pipeline_uuid": pipeline["uuid"], "state": "COMPLETED", "log": "1 passed"})
            merged = call(lead, "bitbucket_merge_pull_request", {"workspace": "acme", "repo_slug": "product", "pull_request_id": pull["id"]})
            self.assertEqual(merged["state"], "MERGED")

            response = InspectorHTTPRouter(system).dispatch(HTTPRequest("GET", f"/api/environments/{environment.id}/workbench"))
            projection = json.loads(response.body)["services"][0]["projection"]
            self.assertEqual(projection["provider"]["id"], "bitbucket")
            repository = projection["repositories"][0]
            self.assertEqual(repository["tickets"][0]["title"], "Refresh race")
            self.assertEqual(repository["changeSets"][0]["reviews"][0]["state"], "APPROVED")
            self.assertEqual(repository["builds"][0]["conclusion"], "success")
            self.assertEqual({item.transport for item in system.list_operations(environment.id)}, {"mcp", "ci-harness"})


@unittest.skipUnless(os.getenv("MCP_SYSTEM_TEST_POSTGRES_DSN"), "set MCP_SYSTEM_TEST_POSTGRES_DSN to run PostgreSQL integration tests")
class BitbucketPostgresTests(unittest.TestCase):
    def test_public_commit_and_issue_persist_in_postgresql(self) -> None:
        dsn = os.environ["MCP_SYSTEM_TEST_POSTGRES_DSN"]
        suffix = uuid4().hex[:10]; control = f"bitbucket_control_{suffix}"; namespace = f"bitbucket_state_{suffix}"
        try:
            with tempfile.TemporaryDirectory() as root:
                registry = PluginRegistry(); registry.register(BitbucketPlugin())
                system = MCPSystem(Path(root), registry, control_plane=PostgresControlPlane(dsn, schema=control), service_storage=PostgresServiceStorage(dsn, namespace=namespace))
                spec = load_template_spec(CONFIG); system.create_template(spec)
                environment = system.create_environment_from_template(spec.template_id)
                dispatcher = MCPDispatcher(system, environment.id, actor="engineer")
                issue = dispatcher.call_tool("bitbucket_create_issue", {"workspace": "acme", "repo_slug": "product", "title": "PostgreSQL ticket"})
                commit = dispatcher.call_tool("bitbucket_create_commit", {"workspace": "acme", "repo_slug": "product", "branch": "main", "message": "Initial", "files": {"README.md": "ready\n"}})
                self.assertFalse(issue["isError"]); self.assertFalse(commit["isError"])
                self.assertEqual(dispatcher.call_tool("bitbucket_list_commits", {"workspace": "acme", "repo_slug": "product"})["structuredContent"]["result"]["size"], 1)
        finally:
            with psycopg.connect(dsn) as connection:
                rows = connection.execute("SELECT nspname FROM pg_namespace WHERE nspname=%s OR nspname LIKE %s", (control, f"{namespace}\\_%")).fetchall()
                for (schema_name,) in rows:
                    connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name)))


if __name__ == "__main__":
    unittest.main()
