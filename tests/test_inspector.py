from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from mcp_system import MCPSystem, PluginRegistry
from mcp_system.config import load_template_spec
from mcp_system.http import HTTPRequest, InspectorHTTPRouter
from mcp_system.service_plugins import GitHubPlugin
from mcp_system.service_plugins.github import GitHubForbidden


CONFIG = Path("configs/templates/github-default.toml")


def registry() -> PluginRegistry:
    result = PluginRegistry()
    result.register(GitHubPlugin())
    return result


class InspectorHTTPRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.system = MCPSystem(Path(self.temp_dir.name), registry())
        template = self.system.create_template(load_template_spec(CONFIG))
        self.environment = self.system.create_environment_from_template(template.id)
        self.system.invoke_service_operation(
            self.environment.id,
            "github",
            actor="engineer",
            transport="mcp",
            operation="get_authenticated_user",
            arguments={},
        )
        with self.assertRaises(GitHubForbidden):
            self.system.invoke_service_operation(
                self.environment.id,
                "github",
                actor="engineer",
                transport="http",
                operation="update_repository",
                arguments={
                    "owner": "acme",
                    "repository": "product",
                    "archived": True,
                },
            )
        self.router = InspectorHTTPRouter(self.system)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_static_application_and_security_headers(self) -> None:
        response = self.router.dispatch(HTTPRequest("GET", "/"))
        self.assertEqual(response.status, 200)
        self.assertIn(b"MCPSystem Inspector", response.body)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])

        script = self.router.dispatch(HTTPRequest("GET", "/assets/app.js"))
        self.assertEqual(script.status, 200)
        self.assertEqual(script.headers["Content-Type"], "text/javascript; charset=utf-8")
        self.assertIn(b"loadOperations", script.body)
        self.assertIn(
            b"await Promise.all([loadOperations(), loadWorkbench()]);",
            script.body,
        )

    def test_environment_and_operation_api(self) -> None:
        environments = self._json(HTTPRequest("GET", "/api/environments"))
        self.assertEqual(len(environments["environments"]), 1)
        self.assertEqual(environments["environments"][0]["id"], self.environment.id)

        operations = self._json(
            HTTPRequest(
                "GET",
                f"/api/environments/{self.environment.id}/operations",
                query={"limit": ("10",)},
            )
        )
        self.assertEqual(
            [item["status"] for item in operations["operations"]],
            ["succeeded", "failed"],
        )
        self.assertEqual(operations["operations"][0]["transport"], "mcp")
        self.assertEqual(operations["operations"][1]["error"]["type"], "forbidden")
        self.assertFalse(operations["truncated"])

    def test_api_rejects_invalid_queries_and_mutation_methods(self) -> None:
        invalid = self.router.dispatch(
            HTTPRequest(
                "GET",
                f"/api/environments/{self.environment.id}/operations",
                query={"limit": ("many",)},
            )
        )
        self.assertEqual(invalid.status, 400)
        self.assertIn("integer", json.loads(invalid.body)["message"])

        mutation = self.router.dispatch(HTTPRequest("POST", "/api/environments"))
        self.assertEqual(mutation.status, 405)

    def test_universal_workbench_projects_ticket_changeset_review_and_diff(self) -> None:
        issue = self._invoke(
            "engineer",
            "create_issue",
            owner="acme",
            repository="product",
            title="Session expires during active work",
            body="Keep active sessions alive.",
            labels=["bug"],
            assignees=["engineer"],
        )
        base = self._invoke(
            "engineer",
            "create_commit",
            owner="acme",
            repository="product",
            message="Add session module",
            author="engineer",
            files={"session.py": "TIMEOUT = 30\n"},
        )
        self._invoke(
            "engineer",
            "create_branch",
            owner="acme",
            repository="product",
            name="main",
            head_sha=base["sha"],
        )
        head = self._invoke(
            "engineer",
            "create_commit",
            owner="acme",
            repository="product",
            message="Refresh active sessions",
            author="engineer",
            parent_shas=[base["sha"]],
            files={"session.py": "TIMEOUT = 60\n"},
        )
        self._invoke(
            "engineer",
            "create_branch",
            owner="acme",
            repository="product",
            name="fix/session-refresh",
            head_sha=head["sha"],
        )
        pull = self._invoke(
            "engineer",
            "create_pull_request",
            owner="acme",
            repository="product",
            title="Refresh active sessions",
            head="fix/session-refresh",
            base="main",
            body="Extends the active session window.",
        )
        self.system.invoke_service_operation(
            self.environment.id,
            "github",
            actor="qa",
            transport="mcp",
            operation="create_review",
            arguments={
                "owner": "acme",
                "repository": "product",
                "pull_number": pull["number"],
                "event": "APPROVE",
                "body": "Verified the new behavior.",
            },
        )
        operation_count = len(self.system.list_operations(self.environment.id))

        workbench = self._json(
            HTTPRequest(
                "GET", f"/api/environments/{self.environment.id}/workbench"
            )
        )
        repository = workbench["services"][0]["projection"]["repositories"][0]
        self.assertEqual(repository["tickets"][0]["id"], str(issue["id"]))
        self.assertEqual(repository["tickets"][0]["labels"], ["bug"])
        change_set = repository["changeSets"][0]
        self.assertEqual(change_set["number"], pull["number"])
        self.assertEqual(change_set["reviews"][0]["state"], "APPROVED")
        self.assertTrue(change_set["diff"]["available"])
        self.assertIn("+TIMEOUT = 60", change_set["diff"]["patch"])
        self.assertEqual(
            len(self.system.list_operations(self.environment.id)), operation_count
        )

    def _invoke(self, actor: str, operation: str, **arguments: object) -> object:
        return self.system.invoke_service_operation(
            self.environment.id,
            "github",
            actor=actor,
            transport="mcp",
            operation=operation,
            arguments=arguments,
        )

    def _json(self, request: HTTPRequest) -> object:
        response = self.router.dispatch(request)
        self.assertEqual(response.status, 200)
        return json.loads(response.body)


if __name__ == "__main__":
    unittest.main()
