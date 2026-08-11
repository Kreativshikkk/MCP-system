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
from mcp_system.service_plugins import JiraPlugin


CONFIG = Path("configs/templates/jira-default.toml")


class JiraPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        registry = PluginRegistry()
        registry.register(JiraPlugin())
        self.system = MCPSystem(Path(self.temp_dir.name), registry)
        template = self.system.create_template(load_template_spec(CONFIG))
        self.environment = self.system.create_environment_from_template(template.id)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def call(self, actor: str, operation: str, **arguments: object) -> object:
        return self.system.invoke_service_operation(
            self.environment.id, "jira", actor=actor, transport="mcp",
            operation=operation, arguments=arguments,
        )

    def test_issue_comment_transition_link_and_inspector_workflow(self) -> None:
        bug = self.call("lead", "create_issue", project="PROD", summary="Session timeout", issue_type="Bug", priority="High", assignee="engineer", labels=["bug", "backend"])
        task = self.call("lead", "create_issue", project="PROD", summary="Add regression coverage", assignee="qa", labels=["testing"])
        self.assertEqual(bug["key"], "PROD-1")
        self.call("engineer", "add_comment", issue_id_or_key=bug["key"], body="Reproduced and preparing a fix.")
        available = self.call("engineer", "list_transitions", issue_id_or_key=bug["key"])
        self.assertEqual({item["id"] for item in available["transitions"]}, {"21", "31"})
        active = self.call("engineer", "transition_issue", issue_id_or_key=bug["key"], transition_id="21")
        self.assertEqual(active["fields"]["status"]["name"], "In Progress")
        link = self.call("lead", "create_issue_link", link_type="Blocks", outward_issue=task["key"], inward_issue=bug["key"])
        self.assertEqual(link["inwardIssue"]["key"], "PROD-1")

        response = InspectorHTTPRouter(self.system).dispatch(HTTPRequest("GET", f"/api/environments/{self.environment.id}/workbench"))
        self.assertEqual(response.status, 200)
        projection = json.loads(response.body)["services"][0]["projection"]
        self.assertEqual(projection["provider"]["id"], "jira")
        tickets = projection["repositories"][0]["tickets"]
        self.assertEqual(tickets[1]["stateReason"], "In Progress")
        self.assertEqual(tickets[1]["comments"][0]["author"], "Software Engineer")
        self.assertEqual(tickets[0]["links"][0]["issueKey"], "PROD-1")

    def test_sprint_lifecycle_and_issue_assignment(self) -> None:
        issue = self.call("lead", "create_issue", project="PROD", summary="Deliver onboarding", issue_type="Story")
        boards = self.call("lead", "list_boards", project="PROD")
        sprint = self.call("lead", "create_sprint", board_id=boards["values"][0]["id"], name="Sprint 1", goal="Ship onboarding")
        moved = self.call("lead", "move_issues_to_sprint", sprint_id=sprint["id"], issues=[issue["key"]])
        self.assertEqual(moved["issues"], ["PROD-1"])
        active = self.call("lead", "update_sprint", sprint_id=sprint["id"], state="active")
        self.assertEqual(active["state"], "active")
        closed = self.call("lead", "update_sprint", sprint_id=sprint["id"], state="closed")
        self.assertEqual(closed["state"], "closed")
        self.assertIsNotNone(closed["completeDate"])

    def test_mcp_surface_is_bound_and_audited(self) -> None:
        dispatcher = MCPDispatcher(self.system, self.environment.id, actor="engineer", bindings={"jira_rest_v3": "jira"})
        names = {tool["name"] for tool in dispatcher.list_tools()}
        self.assertEqual(len(names), 22)
        self.assertIn("jira_create_issue", names)
        self.assertIn("jira_transition_issue", names)
        self.assertIn("jira_move_issues_to_sprint", names)
        result = dispatcher.call_tool("jira_create_issue", {"fields": {"project": {"key": "PROD"}, "summary": "MCP-created ticket", "issuetype": {"name": "Task"}, "assignee": {"accountId": "engineer"}}})
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["result"]["key"], "PROD-1")
        operation = self.system.list_operations(self.environment.id)[-1]
        self.assertEqual(operation.plugin_id, "jira")
        self.assertEqual(operation.operation, "create_issue")

    def test_search_and_validation_errors(self) -> None:
        self.call("lead", "create_issue", project="PROD", summary="Timeout in checkout", issue_type="Bug", assignee="engineer")
        results = self.call("qa", "search_issues", jql="project = PROD and assignee = engineer")
        self.assertEqual([issue["key"] for issue in results["issues"]], ["PROD-1"])
        dispatcher = MCPDispatcher(self.system, self.environment.id, actor="engineer", bindings={"jira_rest_v3": "jira"})
        invalid = dispatcher.call_tool("jira_create_issue", {"fields": {"project": {"key": "PROD"}, "summary": "", "issuetype": {"name": "Incident"}}})
        self.assertTrue(invalid["isError"])
        self.assertEqual(invalid["structuredContent"]["error"]["type"], "validation_failed")

    def test_native_adf_and_agile_read_surface(self) -> None:
        dispatcher = MCPDispatcher(self.system, self.environment.id, actor="lead", bindings={"jira_rest_v3": "jira"})
        adf = {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Timeout under concurrent refresh."}]}]}
        created = dispatcher.call_tool("jira_create_issue", {"fields": {"project": {"key": "PROD"}, "summary": "Refresh race", "description": adf, "issuetype": {"id": "10003"}, "priority": {"id": "2"}, "assignee": {"accountId": "engineer"}, "labels": ["bug"]}})
        self.assertFalse(created["isError"])
        issue = dispatcher.call_tool("jira_get_issue", {"issueIdOrKey": "PROD-1"})["structuredContent"]["result"]
        self.assertEqual(issue["fields"]["description"], adf)
        comment = dispatcher.call_tool("jira_add_comment", {"issueIdOrKey": "PROD-1", "body": adf})
        self.assertEqual(comment["structuredContent"]["result"]["body"], adf)
        board = dispatcher.call_tool("jira_list_boards", {"projectKeyOrId": "PROD"})["structuredContent"]["result"]["values"][0]
        sprint = dispatcher.call_tool("jira_create_sprint", {"originBoardId": board["id"], "name": "Sprint 1", "goal": "Fix refresh"})["structuredContent"]["result"]
        dispatcher.call_tool("jira_move_issues_to_sprint", {"sprintId": sprint["id"], "issues": ["PROD-1"]})
        listed = dispatcher.call_tool("jira_list_sprints", {"boardId": board["id"]})["structuredContent"]["result"]
        self.assertEqual(listed["values"][0]["name"], "Sprint 1")
        sprint_issues = dispatcher.call_tool("jira_list_sprint_issues", {"sprintId": sprint["id"]})["structuredContent"]["result"]
        self.assertEqual(sprint_issues["issues"][0]["key"], "PROD-1")

    def test_template_clone_isolation_and_restart(self) -> None:
        self.call("lead", "create_issue", project="PROD", summary="Only in first clone")
        second = self.system.create_environment_from_template("jira_default")
        second_issues = self.system.invoke_service_operation(second.id, "jira", actor="lead", transport="mcp", operation="search_issues", arguments={"jql": "project = PROD"})
        self.assertEqual(second_issues["issues"], [])
        registry = PluginRegistry(); registry.register(JiraPlugin())
        restarted = MCPSystem(Path(self.temp_dir.name), registry)
        persisted = restarted.invoke_service_operation(self.environment.id, "jira", actor="lead", transport="mcp", operation="get_issue", arguments={"issue_id_or_key": "PROD-1"})
        self.assertEqual(persisted["key"], "PROD-1")


@unittest.skipUnless(os.getenv("MCP_SYSTEM_TEST_POSTGRES_DSN"), "set MCP_SYSTEM_TEST_POSTGRES_DSN to run PostgreSQL integration tests")
class JiraPluginPostgresTests(unittest.TestCase):
    def test_provision_clone_and_identity_sequence(self) -> None:
        dsn = os.environ["MCP_SYSTEM_TEST_POSTGRES_DSN"]
        suffix = uuid4().hex[:10]
        control = f"jira_control_{suffix}"
        namespace = f"jira_state_{suffix}"
        try:
            with tempfile.TemporaryDirectory() as root:
                registry = PluginRegistry(); registry.register(JiraPlugin())
                system = MCPSystem.with_postgres(Path(root), registry, dsn, control_schema=control, storage_namespace=namespace)
                template = system.create_template(load_template_spec(CONFIG))
                first = system.create_environment_from_template(template.id)
                second = system.create_environment_from_template(template.id)
                issue = system.invoke_service_operation(first.id, "jira", actor="lead", transport="mcp", operation="create_issue", arguments={"project": "PROD", "summary": "PostgreSQL issue"})
                empty = system.invoke_service_operation(second.id, "jira", actor="lead", transport="mcp", operation="search_issues", arguments={"jql": "project = PROD"})
                self.assertEqual(issue["key"], "PROD-1")
                self.assertEqual(empty["issues"], [])
        finally:
            with psycopg.connect(dsn) as connection:
                rows = connection.execute("SELECT nspname FROM pg_namespace WHERE nspname=%s OR nspname LIKE %s", (control, f"{namespace}\\_%")).fetchall()
                for (schema_name,) in rows:
                    connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name)))


if __name__ == "__main__":
    unittest.main()
