from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql

from mcp_system import MCPSystem, PluginRegistry, TemplateSpec
from mcp_system.config import load_template_spec
from mcp_system.errors import SnapshotNotFoundError
from mcp_system.service_plugins import GitLabPlugin, JiraPlugin


def registry() -> PluginRegistry:
    result = PluginRegistry(); result.register(JiraPlugin()); result.register(GitLabPlugin())
    return result


def company_spec() -> TemplateSpec:
    jira = load_template_spec(Path("configs/templates/jira-default.toml"))
    gitlab = load_template_spec(Path("configs/templates/gitlab-default.toml"))
    return TemplateSpec(template_id="snapshot_company", name="Snapshot company", version="1.0.0", services=(*jira.services, *gitlab.services), mcp_surfaces=("jira_rest_v3", "gitlab_rest_v4"))


def exercise_snapshot_contract(test: unittest.TestCase, system: MCPSystem) -> None:
    spec = company_spec(); system.create_template(spec)
    environment = system.create_environment_from_template(spec.template_id)
    base = system.invoke_service_operation(environment.id, "gitlab", actor="engineer", transport="mcp", operation="create_commit", arguments={"project": "acme/product", "message": "Baseline", "author": "engineer", "files": {"app.py": "VALUE = 1\n"}})
    system.invoke_service_operation(environment.id, "gitlab", actor="engineer", transport="mcp", operation="create_branch", arguments={"project": "acme/product", "branch": "main", "ref": base["sha"]})
    before = system.snapshot_environment(environment.id, name="correct baseline")

    ticket = system.invoke_service_operation(environment.id, "jira", actor="lead", transport="mcp", operation="create_issue", arguments={"fields": {"project": {"key": "PROD"}, "summary": "Change the value", "issuetype": {"name": "Task"}, "assignee": {"accountId": "engineer"}}})
    commit = system.invoke_service_operation(environment.id, "gitlab", actor="engineer", transport="mcp", operation="create_repository_commit", arguments={"project": "acme/product", "branch": "main", "commit_message": f"{ticket['key']} update value", "actions": [{"action": "update", "file_path": "app.py", "content": "VALUE = 2\n"}]})
    after = system.snapshot_environment(environment.id, name="changed state")

    diff = system.diff_snapshots(before.id, after.id)
    test.assertGreater(diff.metadata["operationCursor"]["countDelta"], 0)
    test.assertEqual(diff.services["jira"]["relational"]["summary"]["inserted"], 1)
    test.assertIn("jira_issues", diff.services["jira"]["relational"]["tables"])
    test.assertIn("gitlab_commits", diff.services["gitlab"]["relational"]["tables"])
    ref_change = diff.services["gitlab"]["git"]["repositories"]["1"]["updated"]["refs/heads/main"]
    test.assertEqual(ref_change, {"before": base["sha"], "after": commit["id"]})
    unchanged_diff = system.diff_snapshots(before.id, before.id)
    test.assertEqual(unchanged_diff.metadata["operationCursor"]["countDelta"], 0)
    test.assertTrue(all(value["relational"]["summary"] == {"inserted": 0, "deleted": 0, "updated": 0} for value in unchanged_diff.services.values()))

    baseline_clone = system.create_environment_from_snapshot(before.id, name="Air baseline")
    changed_clone = system.create_environment_from_snapshot(after.id, name="Air changed")
    test.assertEqual(baseline_clone.snapshot_id, before.id)
    baseline_issues = system.invoke_service_operation(baseline_clone.id, "jira", actor="lead", transport="mcp", operation="search_issues", arguments={"jql": "project = PROD"})
    changed_issue = system.invoke_service_operation(changed_clone.id, "jira", actor="lead", transport="mcp", operation="get_issue", arguments={"issue_id_or_key": "PROD-1"})
    test.assertEqual(baseline_issues["issues"], [])
    test.assertEqual(changed_issue["key"], "PROD-1")
    changed_file = system.invoke_service_operation(changed_clone.id, "gitlab", actor="engineer", transport="mcp", operation="get_repository_file", arguments={"project": "acme/product", "file_path": "app.py", "ref": "main"})
    test.assertEqual(changed_file["content"], "VkFMVUUgPSAyCg==")

    system.invoke_service_operation(environment.id, "jira", actor="lead", transport="mcp", operation="create_issue", arguments={"project": "PROD", "summary": "Later mutation"})
    unchanged = system.invoke_service_operation(changed_clone.id, "jira", actor="lead", transport="mcp", operation="search_issues", arguments={"jql": "project = PROD"})
    test.assertEqual([item["key"] for item in unchanged["issues"]], ["PROD-1"])

    restarted = MCPSystem(system.data_root, registry(), control_plane=system.control_plane, service_storage=system.service_storage, git_storage=system.git_storage)
    test.assertEqual(restarted.require_snapshot(after.id).operation_count, after.operation_count)
    restarted_clone = restarted.create_environment_from_snapshot(after.id)
    test.assertEqual(restarted_clone.snapshot_id, after.id)
    with test.assertRaises(SnapshotNotFoundError): restarted.require_snapshot("missing")


class SnapshotSQLiteTests(unittest.TestCase):
    def test_snapshot_clone_diff_restart_and_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            exercise_snapshot_contract(self, MCPSystem(Path(root), registry()))


@unittest.skipUnless(os.getenv("MCP_SYSTEM_TEST_POSTGRES_DSN"), "set MCP_SYSTEM_TEST_POSTGRES_DSN to run PostgreSQL integration tests")
class SnapshotPostgresTests(unittest.TestCase):
    def test_snapshot_clone_diff_restart_and_isolation(self) -> None:
        dsn = os.environ["MCP_SYSTEM_TEST_POSTGRES_DSN"]
        suffix = uuid4().hex[:10]; control = f"snapshot_control_{suffix}"; namespace = f"snapshot_state_{suffix}"
        try:
            with tempfile.TemporaryDirectory() as root:
                exercise_snapshot_contract(self, MCPSystem.with_postgres(Path(root), registry(), dsn, control_schema=control, storage_namespace=namespace))
        finally:
            with psycopg.connect(dsn) as connection:
                rows = connection.execute("SELECT nspname FROM pg_namespace WHERE nspname=%s OR nspname LIKE %s", (control, f"{namespace}\\_%")).fetchall()
                for (schema_name,) in rows:
                    connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name)))


if __name__ == "__main__": unittest.main()
