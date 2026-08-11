from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from mcp_system import MCPSystem, PluginRegistry
from mcp_system.config import load_template_spec
from mcp_system.mcp import MCPDispatcher
from mcp_system.service_plugins import BitbucketPlugin, GitHubPlugin, GitLabPlugin, JiraPlugin, LinearPlugin, YouTrackPlugin


def registry() -> PluginRegistry:
    value = PluginRegistry()
    for plugin in (GitHubPlugin(), GitLabPlugin(), JiraPlugin(), BitbucketPlugin(), LinearPlugin(), YouTrackPlugin()):
        value.register(plugin)
    return value


class ExtendedServicesLifecycleTests(unittest.TestCase):
    def test_company_clone_restart_snapshot_and_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            data_root = Path(root); system = MCPSystem(data_root, registry())
            spec = load_template_spec(Path("configs/templates/software-company-default.toml")); system.create_template(spec)
            first = system.create_environment_from_template(spec.template_id, name="first")
            second = system.create_environment_from_template(spec.template_id, name="second")
            dispatcher = MCPDispatcher(system, first.id, actor="lead")
            calls = (
                ("bitbucket_create_issue", {"workspace":"acme","repo_slug":"product","title":"Bitbucket trace"}),
                ("linear_create_issue", {"teamId":"team-1","title":"Linear trace"}),
                ("youtrack_create_issue", {"projectId":"PROD","summary":"YouTrack trace"}),
            )
            for tool, arguments in calls:
                self.assertFalse(dispatcher.call_tool(tool, arguments)["isError"])
            snapshot = system.snapshot_environment(first.id, name="extended trace")
            clone = system.create_environment_from_snapshot(snapshot.id)
            restarted = MCPSystem(data_root, registry())
            expectations = (
                ("bitbucket_list_issues", {"workspace":"acme","repo_slug":"product"}, "values"),
                ("linear_list_issues", {"teamId":"team-1"}, "nodes"),
                ("youtrack_list_issues", {}, None),
            )
            for environment_id, expected_count in ((first.id, 1), (clone.id, 1), (second.id, 0)):
                current = MCPDispatcher(restarted, environment_id, actor="lead")
                for tool, arguments, collection in expectations:
                    result = current.call_tool(tool, arguments)["structuredContent"]["result"]
                    self.assertEqual(len(result[collection] if collection else result), expected_count)
            self.assertEqual(restarted.diff_snapshots(snapshot.id, snapshot.id).metadata["operationCursor"]["countDelta"], 0)


if __name__ == "__main__":
    unittest.main()
