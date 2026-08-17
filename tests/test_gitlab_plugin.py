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
from mcp_system.http import GitLabHTTPRouter, GitLabTokenActorResolver, HTTPRequest, InspectorHTTPRouter
from mcp_system.mcp import MCPDispatcher
from mcp_system.service_plugins import GitLabPlugin
from mcp_system.storage import PostgresControlPlane, PostgresServiceStorage


CONFIG = Path("configs/templates/gitlab-default.toml")


class GitLabPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        registry = PluginRegistry()
        registry.register(GitLabPlugin())
        self.system = MCPSystem(Path(self.temp_dir.name), registry)
        template = self.system.create_template(load_template_spec(CONFIG))
        self.environment = self.system.create_environment_from_template(template.id)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def call(self, actor: str, operation: str, **arguments: object) -> object:
        return self.system.invoke_service_operation(
            self.environment.id, "gitlab", actor=actor, transport="mcp",
            operation=operation, arguments=arguments,
        )

    def test_issue_merge_request_pipeline_and_universal_projection(self) -> None:
        issue = self.call("engineer", "create_issue", project="acme/product", title="Session timeout", labels=["bug"], assignee="engineer")
        self.call("engineer", "create_issue_note", project="acme/product", issue_iid=issue["iid"], body="Reproduced locally.")
        base = self.call("engineer", "create_commit", project="acme/product", message="Base", author="engineer", files={"app.py": "TIMEOUT = 30\n"})
        self.call("engineer", "create_branch", project="acme/product", branch="main", ref=base["sha"])
        head = self.call("engineer", "create_commit", project="acme/product", message="Fix timeout", author="engineer", parent_shas=[base["sha"]], files={"app.py": "TIMEOUT = 60\n"})
        self.call("engineer", "create_branch", project="acme/product", branch="fix/timeout", ref=head["sha"])
        merge_request = self.call("engineer", "create_merge_request", project="acme/product", title="Fix timeout", source_branch="fix/timeout", target_branch="main", reviewers=["qa"])
        approved = self.call("qa", "approve_merge_request", project="acme/product", merge_request_iid=merge_request["iid"])
        self.assertTrue(approved["approved"])
        pipeline = self.call("engineer", "create_pipeline", project="acme/product", ref="fix/timeout")
        pipeline = self.call("director", "complete_pipeline", project="acme/product", pipeline_id=pipeline["id"], status="success", trace="1 passed\n")
        jobs = self.call("engineer", "list_pipeline_jobs", project="acme/product", pipeline_id=pipeline["id"])
        self.assertEqual(jobs[0]["status"], "success")
        self.assertIsNotNone(jobs[0]["finished_at"])
        self.assertEqual(self.call("engineer", "get_job_trace", project="acme/product", job_id=jobs[0]["id"]), "1 passed\n")

        response = InspectorHTTPRouter(self.system).dispatch(HTTPRequest("GET", f"/api/environments/{self.environment.id}/workbench"))
        self.assertEqual(response.status, 200)
        projection = json.loads(response.body)["services"][0]["projection"]
        self.assertEqual(projection["provider"]["id"], "gitlab")
        project = projection["repositories"][0]
        self.assertEqual(project["tickets"][0]["comments"][0]["body"], "Reproduced locally.")
        self.assertEqual(project["changeSets"][0]["reviews"][0]["state"], "APPROVED")
        self.assertIn("+TIMEOUT = 60", project["changeSets"][0]["diff"]["patch"])
        self.assertEqual(project["builds"][0]["id"], str(pipeline["id"]))
        self.assertEqual(project["builds"][0]["conclusion"], "success")

    def test_mcp_surface_is_selected_by_template(self) -> None:
        dispatcher = MCPDispatcher(self.system, self.environment.id, actor="engineer", bindings={"gitlab_rest_v4": "gitlab"})
        names = {tool["name"] for tool in dispatcher.list_tools()}
        self.assertIn("gitlab_create_merge_request", names)
        self.assertIn("gitlab_list_pipeline_jobs", names)
        self.assertNotIn("github_create_issue", names)
        created = dispatcher.call_tool(
            "gitlab_create_issue",
            {
                "project": "acme/product",
                "title": "REST v4 MCP contract",
                "labels": "bug",
            },
        )
        self.assertFalse(created["isError"])
        self.assertEqual(created["structuredContent"]["result"]["iid"], 1)
        operation = self.system.list_operations(self.environment.id)[-1]
        self.assertEqual(operation.transport, "mcp")
        self.assertEqual(operation.operation, "create_issue")

    def test_complete_pipeline_is_admin_only_and_requires_terminal_status(self) -> None:
        base = self.call("engineer", "create_commit", project="acme/product", message="Base", author="engineer", files={"app.py": "OK = True\n"})
        self.call("engineer", "create_branch", project="acme/product", branch="main", ref=base["sha"])
        pipeline = self.call("engineer", "create_pipeline", project="acme/product", ref="main")

        with self.assertRaisesRegex(Exception, "403 Forbidden"):
            self.call("engineer", "complete_pipeline", project="acme/product", pipeline_id=pipeline["id"], status="success", trace="forged")
        with self.assertRaisesRegex(Exception, "terminal pipeline status"):
            self.call("director", "complete_pipeline", project="acme/product", pipeline_id=pipeline["id"], status="running", trace="still running")

        self.assertEqual(self.call("engineer", "get_pipeline", project="acme/product", pipeline_id=pipeline["id"])["status"], "pending")
        jobs = self.call("engineer", "list_pipeline_jobs", project="acme/product", pipeline_id=pipeline["id"])
        self.assertEqual(jobs[0]["status"], "pending")
        self.assertEqual(self.call("engineer", "get_job_trace", project="acme/product", job_id=jobs[0]["id"]), "")

    def test_public_commit_api_initializes_an_empty_repository(self) -> None:
        dispatcher = MCPDispatcher(self.system, self.environment.id, actor="engineer")
        initial = dispatcher.call_tool(
            "gitlab_create_repository_commit",
            {
                "project": "acme/product",
                "branch": "main",
                "commit_message": "Initial commit",
                "actions": [
                    {
                        "action": "create",
                        "file_path": "README.md",
                        "content": "# Product\n",
                    }
                ],
            },
        )
        self.assertFalse(initial["isError"])
        sha = initial["structuredContent"]["result"]["id"]
        feature = dispatcher.call_tool(
            "gitlab_create_repository_commit",
            {
                "project": "acme/product",
                "branch": "feature/readme",
                "start_branch": "main",
                "commit_message": "Expand readme",
                "actions": [
                    {
                        "action": "update",
                        "file_path": "README.md",
                        "content": "# Product\nReady.\n",
                    }
                ],
            },
        )
        self.assertFalse(feature["isError"])
        self.assertEqual(
            self.system.open_git_data_plane(
                self.environment.id, "gitlab"
            ).repository(1).resolve_branch("main"),
            sha,
        )

    def test_openapi_shaped_repository_review_ci_and_release_flow(self) -> None:
        base = self.call("engineer", "create_commit", project="acme/product", message="Base", author="engineer", files={"README.md": "base\n"})
        self.call("engineer", "create_branch", project="acme/product", branch="main", ref=base["sha"])
        self.call("engineer", "create_branch", project="acme/product", branch="feature", ref=base["sha"])
        commit = self.call("engineer", "create_repository_commit", project="acme/product", branch="feature", commit_message="Implement feature", actions=[{"action": "create", "file_path": "feature.txt", "content": "ready\n"}])
        file = self.call("engineer", "get_repository_file", project="acme/product", file_path="feature.txt", ref="feature")
        self.assertEqual(file["commit_id"], commit["id"])
        self.assertEqual(file["encoding"], "base64")

        merge_request = self.call("engineer", "create_merge_request", project="acme/product", title="Feature", source_branch="feature", target_branch="main", reviewers=["qa"])
        discussion = self.call("qa", "create_merge_request_discussion", project="acme/product", merge_request_iid=merge_request["iid"], body="Please verify the edge case.")
        resolved = self.call("engineer", "resolve_merge_request_discussion", project="acme/product", merge_request_iid=merge_request["iid"], discussion_id=discussion["id"], resolved=True)
        self.assertTrue(resolved["notes"][0]["resolved"])
        approval = self.call("qa", "approve_merge_request", project="acme/product", merge_request_iid=merge_request["iid"])
        self.assertTrue(approval["approved"])
        merged = self.call("lead", "merge_merge_request", project="acme/product", merge_request_iid=merge_request["iid"], sha=commit["id"])
        self.assertEqual(merged["state"], "merged")

        status = self.call("engineer", "set_commit_status", project="acme/product", sha=merged["merge_commit_sha"], state="success", name="test")
        self.assertEqual(status["status"], "success")
        pipeline = self.call("engineer", "create_pipeline", project="acme/product", ref="main")
        jobs = self.call("engineer", "list_pipeline_jobs", project="acme/product", pipeline_id=pipeline["id"])
        self.assertEqual(jobs[0]["name"], "test")
        tag = self.call("engineer", "create_tag", project="acme/product", tag_name="v1.0.0", ref="main")
        release = self.call("engineer", "create_release", project="acme/product", tag_name=tag["name"], name="Version 1.0.0")
        self.assertEqual(release["tag_name"], "v1.0.0")

    def test_gitlab_http_v4_uses_real_paths_and_parameters(self) -> None:
        router = GitLabHTTPRouter(self.system, self.environment.id, actor="engineer")
        current = router.dispatch(HTTPRequest("GET", "/api/v4/user"))
        self.assertEqual(current.status, 200)
        self.assertEqual(json.loads(current.body)["username"], "engineer")
        users = router.dispatch(HTTPRequest("GET", "/api/v4/users", query={"username": ("qa",)}))
        self.assertEqual([item["username"] for item in json.loads(users.body)], ["qa"])
        created = router.dispatch(HTTPRequest("POST", "/api/v4/projects/acme%2Fproduct/issues", headers={"Content-Type": "application/json"}, body=json.dumps({"title": "HTTP issue", "labels": "bug"}).encode()))
        self.assertEqual(created.status, 201)
        self.assertEqual(json.loads(created.body)["iid"], 1)
        missing = router.dispatch(HTTPRequest("POST", "/api/v4/projects/acme%2Fproduct/issues", headers={"Content-Type": "application/json"}, body=b"{}"))
        self.assertEqual(missing.status, 400)

    def test_gitlab_pagination_authentication_and_error_mapping(self) -> None:
        router = GitLabHTTPRouter(
            self.system, self.environment.id,
            actor_resolver=GitLabTokenActorResolver({"engineer-token": "engineer"}),
        )
        unauthorized = router.dispatch(HTTPRequest("GET", "/api/v4/user"))
        self.assertEqual(unauthorized.status, 401)
        headers = {"PRIVATE-TOKEN": "engineer-token", "Content-Type": "application/json"}
        for number in range(3):
            response = router.dispatch(HTTPRequest("POST", "/api/v4/projects/acme%2Fproduct/issues", headers=headers, body=json.dumps({"title": f"Issue {number}"}).encode()))
            self.assertEqual(response.status, 201)
        page = router.dispatch(HTTPRequest("GET", "/api/v4/projects/acme%2Fproduct/issues", headers=headers, query={"page": ("2",), "per_page": ("2",)}))
        self.assertEqual(page.status, 200)
        self.assertEqual(len(json.loads(page.body)), 1)
        self.assertEqual(page.headers["X-Total"], "3")
        self.assertEqual(page.headers["X-Total-Pages"], "2")
        self.assertEqual(page.headers["X-Prev-Page"], "1")
        self.assertEqual(page.headers["X-Next-Page"], "")
        invalid_page = router.dispatch(HTTPRequest("GET", "/api/v4/projects/acme%2Fproduct/issues", headers=headers, query={"page": ("zero",)}))
        self.assertEqual(invalid_page.status, 400)
        not_found = router.dispatch(HTTPRequest("GET", "/api/v4/projects/missing" , headers=headers))
        self.assertEqual(not_found.status, 404)
        unsupported_media = router.dispatch(HTTPRequest("POST", "/api/v4/projects/acme%2Fproduct/issues", headers={"PRIVATE-TOKEN": "engineer-token", "Content-Type": "text/plain"}, body=b"title=x"))
        self.assertEqual(unsupported_media.status, 415)


@unittest.skipUnless(
    os.getenv("MCP_SYSTEM_TEST_POSTGRES_DSN"),
    "set MCP_SYSTEM_TEST_POSTGRES_DSN to run PostgreSQL integration tests",
)
class GitLabPluginPostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dsn = os.environ["MCP_SYSTEM_TEST_POSTGRES_DSN"]
        suffix = uuid4().hex[:10]
        self.control_schema = f"gitlab_control_{suffix}"
        self.storage_namespace = f"gitlab_state_{suffix}"
        self.temp_dir = tempfile.TemporaryDirectory()
        registry = PluginRegistry()
        registry.register(GitLabPlugin())
        self.system = MCPSystem(
            Path(self.temp_dir.name), registry,
            control_plane=PostgresControlPlane(self.dsn, schema=self.control_schema),
            service_storage=PostgresServiceStorage(self.dsn, namespace=self.storage_namespace),
        )

    def tearDown(self) -> None:
        with psycopg.connect(self.dsn) as connection:
            rows = connection.execute(
                "SELECT nspname FROM pg_namespace WHERE nspname=%s OR nspname LIKE %s",
                (self.control_schema, f"{self.storage_namespace}\\_%"),
            ).fetchall()
            for (schema_name,) in rows:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name)))
        self.temp_dir.cleanup()

    def test_materialize_and_issue_operation(self) -> None:
        template = self.system.create_template(load_template_spec(CONFIG))
        environment = self.system.create_environment_from_template(template.id)
        issue = self.system.invoke_service_operation(
            environment.id, "gitlab", actor="engineer", transport="mcp",
            operation="create_issue",
            arguments={"project": "acme/product", "title": "PostgreSQL issue", "labels": ["bug"]},
        )
        self.assertEqual(issue["iid"], 1)
        def invoke(operation: str, **arguments: object) -> object:
            return self.system.invoke_service_operation(
                environment.id, "gitlab", actor="engineer", transport="mcp",
                operation=operation, arguments=arguments,
            )
        invoke("create_repository_commit", project="acme/product", branch="main", commit_message="Base", actions=[{"action": "create", "file_path": "README.md", "content": "base\n"}])
        invoke("create_repository_commit", project="acme/product", branch="main", commit_message="Add source", actions=[{"action": "create", "file_path": "src.py", "content": "VALUE = 1\n"}])
        pipeline = invoke("create_pipeline", project="acme/product", ref="main")
        self.assertEqual(invoke("list_pipeline_jobs", project="acme/product", pipeline_id=pipeline["id"])[0]["name"], "test")
        invoke("create_tag", project="acme/product", tag_name="v1", ref="main")
        self.assertEqual(invoke("create_release", project="acme/product", tag_name="v1")["tag_name"], "v1")
        response = InspectorHTTPRouter(self.system).dispatch(
            HTTPRequest("GET", f"/api/environments/{environment.id}/workbench")
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body)["services"][0]["projection"]["provider"]["id"], "gitlab")


if __name__ == "__main__":
    unittest.main()
