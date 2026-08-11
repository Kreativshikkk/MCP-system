from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from mcp_system import MCPSystem, OperationRecord, OperationStatus, PluginRegistry
from mcp_system.config import load_template_spec
from mcp_system.http import GitHubHTTPRouter
from mcp_system.http.base import HTTPRequest
from mcp_system.mcp import MCPDispatcher
from mcp_system.service_plugins import GitHubPlugin


CONFIG = Path("configs/templates/github-default.toml")


class SequentialOperationIds:
    def __init__(self) -> None:
        self.value = 0

    def new_operation_id(self) -> str:
        self.value += 1
        return f"op{self.value:04d}"


def registry() -> PluginRegistry:
    result = PluginRegistry()
    result.register(GitHubPlugin())
    return result


class OperationLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temp_dir.name)
        self.registry = registry()
        self.system = MCPSystem(
            self.data_root,
            self.registry,
            operation_ids=SequentialOperationIds(),
        )
        template = self.system.create_template(load_template_spec(CONFIG))
        self.environment = self.system.create_environment_from_template(template.id)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_mcp_and_http_attempts_survive_restart(self) -> None:
        dispatcher = MCPDispatcher(
            self.system,
            self.environment.id,
            actor="engineer",
            bindings={"github_rest_v3": "github"},
        )
        authenticated = dispatcher.call_tool("github_get_authenticated_user", {})
        self.assertFalse(authenticated["isError"])

        forbidden = dispatcher.call_tool(
            "github_update_repository",
            {"owner": "acme", "repo": "product", "archived": True},
        )
        self.assertTrue(forbidden["isError"])

        invalid = dispatcher.call_tool(
            "github_create_issue",
            {
                "owner": "acme",
                "repo": "product",
                "title": "invalid before provider dispatch",
                "unexpected": True,
            },
        )
        self.assertTrue(invalid["isError"])

        response = GitHubHTTPRouter(
            self.system, self.environment.id, actor="qa"
        ).dispatch(HTTPRequest("GET", "/repos/acme/product/issues"))
        self.assertEqual(response.status, 200)

        restarted = MCPSystem(self.data_root, self.registry)
        operations = restarted.list_operations(self.environment.id)
        self.assertEqual(
            [item.id for item in operations], ["op0001", "op0002", "op0003"]
        )
        self.assertEqual(
            [item.transport for item in operations], ["mcp", "mcp", "http"]
        )
        self.assertEqual(
            [item.status for item in operations],
            [
                OperationStatus.SUCCEEDED,
                OperationStatus.FAILED,
                OperationStatus.SUCCEEDED,
            ],
        )
        self.assertEqual(operations[0].actor, "engineer")
        self.assertEqual(operations[0].operation, "get_authenticated_user")
        self.assertEqual(operations[0].result["login"], "engineer")
        self.assertEqual(operations[1].error["type"], "forbidden")
        self.assertEqual(operations[1].error["status"], 403)
        self.assertEqual(operations[2].actor, "qa")
        self.assertEqual(operations[2].request["owner"], "acme")

    def test_restart_marks_in_flight_attempt_interrupted(self) -> None:
        started_at = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
        self.system.control_plane.begin_operation(
            OperationRecord(
                id="abandoned-operation",
                environment_id=self.environment.id,
                service_instance_id="github",
                plugin_id="github",
                actor="engineer",
                transport="mcp",
                operation="create_issue",
                request={"title": "never completed"},
                status=OperationStatus.RUNNING,
                started_at=started_at,
            )
        )

        restarted = MCPSystem(self.data_root, self.registry)
        operation = restarted.list_operations(self.environment.id)[0]
        self.assertEqual(operation.status, OperationStatus.INTERRUPTED)
        self.assertEqual(operation.error["type"], "interrupted")
        self.assertIsNotNone(operation.completed_at)


if __name__ == "__main__":
    unittest.main()
