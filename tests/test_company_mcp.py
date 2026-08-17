from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mcp_system import MCPSystem, PluginRegistry, builtin_plugin_registry
from mcp_system.config import load_template_spec
from mcp_system.mcp import MCPDispatcher
from mcp_system.mcp.launcher import build_client_config
from mcp_system.service_plugins import BitbucketPlugin, GitHubPlugin, GitLabPlugin, JiraPlugin, LinearPlugin, YouTrackPlugin


CONFIG = Path("configs/templates/software-company-default.toml")


def registry() -> PluginRegistry:
    return builtin_plugin_registry()


def call(
    case: unittest.TestCase,
    dispatcher: MCPDispatcher,
    name: str,
    arguments: dict[str, object],
) -> object:
    response = dispatcher.call_tool(name, arguments)
    case.assertFalse(response["isError"], response["structuredContent"])
    return response["structuredContent"]["result"]


def minimal_schema_value(schema: dict[str, object]) -> object:
    if "enum" in schema:
        return schema["enum"][0]
    expected = schema.get("type")
    if isinstance(expected, list):
        expected = next(item for item in expected if item != "null")
    if expected == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        return {
            key: minimal_schema_value(properties[key])
            for key in required
        }
    if expected == "array":
        return []
    if expected == "integer":
        return max(1, int(schema.get("minimum", 1)))
    if expected == "number":
        return 1
    if expected == "boolean":
        return False
    if expected == "null":
        return None
    return "missing"


class CompanyMCPTests(unittest.TestCase):
    def test_all_company_tools_keep_schema_valid_failures_inside_tool_results(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            system = MCPSystem(Path(root), registry())
            spec = load_template_spec(CONFIG)
            system.create_template(spec)
            environment = system.create_environment_from_template(spec.template_id)
            dispatcher = MCPDispatcher(system, environment.id, actor="engineer")
            escaped: list[str] = []
            for tool in dispatcher.list_tools():
                arguments = minimal_schema_value(tool["inputSchema"])
                try:
                    response = dispatcher.call_tool(tool["name"], arguments)
                except Exception as exc:  # This gate intentionally audits the public boundary.
                    escaped.append(f"{tool['name']}: {type(exc).__name__}: {exc}")
                    continue
                self.assertIn("isError", response, tool["name"])
                self.assertIn("structuredContent", response, tool["name"])
            self.assertEqual(escaped, [])

    def test_generated_client_entry_runs_all_company_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            data_root = Path(root)
            system = MCPSystem(data_root, registry())
            spec = load_template_spec(CONFIG)
            system.create_template(spec)
            environment = system.create_environment_from_template(spec.template_id)
            config = build_client_config(
                environment_id=environment.id,
                actors=("engineer",),
                data_root=data_root,
                python_executable=Path(sys.executable),
                server_script=Path("scripts/mcp_server.py"),
            )
            server = config["mcpServers"]["mcp-system-engineer"]
            messages = (
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "company-smoke", "version": "1"},
                    },
                },
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            )
            process = subprocess.run(
                (server["command"], *server["args"]),
                input="".join(json.dumps(message) + "\n" for message in messages),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=15,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            responses = [json.loads(line) for line in process.stdout.splitlines()]
            tools = {tool["name"] for tool in responses[1]["result"]["tools"]}
            # 222 provider operations minus the two CI verdict writers
            # (gitlab set_commit_status, bitbucket create_commit_status)
            self.assertEqual(len(tools), 233)
            self.assertTrue(
                {"github_get_authenticated_user", "gitlab_get_current_user", "jira_get_current_user", "bitbucket_get_current_user", "linear_get_viewer", "youtrack_get_current_user"}
                <= tools
            )

    def test_multi_actor_ticket_code_review_ci_and_merge_through_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            system = MCPSystem(Path(root), registry())
            spec = load_template_spec(CONFIG)
            system.create_template(spec)
            environment = system.create_environment_from_template(spec.template_id)
            lead = MCPDispatcher(system, environment.id, actor="lead")
            engineer = MCPDispatcher(system, environment.id, actor="engineer")
            qa = MCPDispatcher(system, environment.id, actor="qa")

            ticket = call(
                self,
                lead,
                "jira_create_issue",
                {
                    "fields": {
                        "project": {"key": "PROD"},
                        "summary": "Fix refresh race",
                        "issuetype": {"name": "Bug"},
                        "assignee": {"accountId": "engineer"},
                        "labels": ["bug"],
                    }
                },
            )
            call(
                self,
                engineer,
                "jira_transition_issue",
                {"issueIdOrKey": ticket["key"], "transition": {"id": "21"}},
            )
            base = call(
                self,
                engineer,
                "gitlab_create_repository_commit",
                {
                    "project": "acme/product",
                    "branch": "main",
                    "commit_message": "Initial commit",
                    "actions": [
                        {"action": "create", "file_path": "app.py", "content": "SAFE = False\n"}
                    ],
                },
            )
            head = call(
                self,
                engineer,
                "gitlab_create_repository_commit",
                {
                    "project": "acme/product",
                    "branch": f"fix/{ticket['key'].lower()}",
                    "start_sha": base["id"],
                    "commit_message": f"{ticket['key']} fix refresh race",
                    "actions": [
                        {"action": "update", "file_path": "app.py", "content": "SAFE = True\n"}
                    ],
                },
            )
            merge_request = call(
                self,
                engineer,
                "gitlab_create_merge_request",
                {
                    "project": "acme/product",
                    "title": f"{ticket['key']} Fix refresh race",
                    "source_branch": f"fix/{ticket['key'].lower()}",
                    "target_branch": "main",
                    "reviewer_ids": [4],
                },
            )
            changes = call(
                self,
                qa,
                "gitlab_get_merge_request_changes",
                {"project": "acme/product", "merge_request_iid": merge_request["iid"]},
            )
            self.assertIn("+SAFE = True", changes["changes"][0]["diff"])
            call(
                self,
                qa,
                "gitlab_approve_merge_request",
                {"project": "acme/product", "merge_request_iid": merge_request["iid"]},
            )
            pipeline = call(
                self,
                engineer,
                "gitlab_create_merge_request_pipeline",
                {"project": "acme/product", "merge_request_iid": merge_request["iid"]},
            )
            system.invoke_service_operation(
                environment.id,
                "gitlab",
                actor="director",
                transport="ci-harness",
                operation="complete_pipeline",
                arguments={"project": "acme/product", "pipeline_id": pipeline["id"], "status": "success", "trace": "1 passed\n"},
            )
            merged = call(
                self,
                lead,
                "gitlab_merge_merge_request",
                {
                    "project": "acme/product",
                    "merge_request_iid": merge_request["iid"],
                    "sha": head["id"],
                },
            )
            done = call(
                self,
                lead,
                "jira_transition_issue",
                {"issueIdOrKey": ticket["key"], "transition": {"id": "31"}},
            )
            github_actor = call(
                self, engineer, "github_get_authenticated_user", {}
            )

            self.assertEqual(merged["state"], "merged")
            self.assertEqual(done["fields"]["status"]["name"], "Done")
            self.assertEqual(github_actor["login"], "engineer")
            operations = system.list_operations(environment.id, limit=100)
            self.assertEqual(
                {item.plugin_id for item in operations}, {"github", "gitlab", "jira"}
            )
            self.assertEqual(
                next(item for item in operations if item.operation == "complete_pipeline").transport,
                "ci-harness",
            )

    def test_agent_engineering_smoke_jira_bitbucket_reciprocal_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            system = MCPSystem(Path(root), registry())
            spec = load_template_spec(CONFIG)
            system.create_template(spec)
            environment = system.create_environment_from_template(spec.template_id)
            lead = MCPDispatcher(system, environment.id, actor="lead")
            engineer = MCPDispatcher(system, environment.id, actor="engineer")

            base = call(
                self,
                lead,
                "bitbucket_create_commit",
                {
                    "workspace": "acme",
                    "repo_slug": "product",
                    "branch": "main",
                    "message": "Initialize calculator",
                    "files": {
                        "calculator.py": "def divide(a, b):\n    return a * b\n",
                        "README.md": "divide(a, b) returns a / b and rejects zero with ValueError.\n",
                    },
                },
            )
            bug = call(
                self,
                lead,
                "jira_create_issue",
                {
                    "fields": {
                        "project": {"key": "PROD"},
                        "summary": "Correct divide behavior",
                        "issuetype": {"name": "Bug"},
                        "assignee": {"accountId": "engineer"},
                        "description": "In acme/product, divide must return a / b and raise ValueError for zero.",
                    }
                },
            )
            call(
                self,
                lead,
                "jira_add_comment",
                {
                    "issueIdOrKey": bug["key"],
                    "body": {
                        "type": "doc",
                        "version": 1,
                        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Ready for implementation."}]}],
                    },
                },
            )

            assigned = call(
                self,
                engineer,
                "jira_get_issue",
                {"issueIdOrKey": bug["key"]},
            )
            self.assertEqual(assigned["fields"]["assignee"]["accountId"], "engineer")
            call(
                self,
                engineer,
                "jira_transition_issue",
                {"issueIdOrKey": bug["key"], "transition": {"id": "21"}},
            )
            head = call(
                self,
                engineer,
                "bitbucket_create_commit",
                {
                    "workspace": "acme",
                    "repo_slug": "product",
                    "branch": f"fix/{bug['key']}",
                    "message": f"{bug['key']} fix divide",
                    "parents": [base["hash"]],
                    "files": {
                        "calculator.py": (
                            "def divide(a, b):\n"
                            "    if b == 0:\n"
                            "        raise ValueError(\"divisor must not be zero\")\n"
                            "    return a / b\n"
                        ),
                        "test_calculator.py": (
                            "import pytest\n"
                            "from calculator import divide\n\n"
                            "def test_divide():\n"
                            "    assert divide(8, 2) == 4\n"
                            "    with pytest.raises(ValueError): divide(8, 0)\n"
                        ),
                    },
                },
            )
            pull = call(
                self,
                engineer,
                "bitbucket_create_pull_request",
                {
                    "workspace": "acme",
                    "repo_slug": "product",
                    "title": f"{bug['key']} Correct divide behavior",
                    "source_branch": f"fix/{bug['key']}",
                    "destination_branch": "main",
                    "reviewers": ["lead"],
                },
            )
            review_task = call(
                self,
                engineer,
                "jira_create_issue",
                {
                    "fields": {
                        "project": {"key": "PROD"},
                        "summary": f"Review acme/product PR #{pull['id']}",
                        "issuetype": {"name": "Task"},
                        "assignee": {"accountId": "lead"},
                        "description": f"Review PR #{pull['id']} against {bug['key']} and merge if valid.",
                    }
                },
            )

            assigned_review = call(
                self,
                lead,
                "jira_get_issue",
                {"issueIdOrKey": review_task["key"]},
            )
            self.assertEqual(assigned_review["fields"]["reporter"]["accountId"], "engineer")
            diff = call(
                self,
                lead,
                "bitbucket_get_pull_request_diff",
                {"workspace": "acme", "repo_slug": "product", "pull_request_id": pull["id"]},
            )
            self.assertIn("+    return a / b", diff["patch"])
            self.assertIn("+        raise ValueError", diff["patch"])
            call(
                self,
                lead,
                "bitbucket_create_pull_request_comment",
                {
                    "workspace": "acme",
                    "repo_slug": "product",
                    "pull_request_id": pull["id"],
                    "content": "Acceptance criteria and regression coverage verified.",
                },
            )
            call(
                self,
                lead,
                "bitbucket_approve_pull_request",
                {"workspace": "acme", "repo_slug": "product", "pull_request_id": pull["id"]},
            )
            merged = call(
                self,
                lead,
                "bitbucket_merge_pull_request",
                {"workspace": "acme", "repo_slug": "product", "pull_request_id": pull["id"]},
            )
            for issue_key in (bug["key"], review_task["key"]):
                call(
                    self,
                    lead,
                    "jira_add_comment",
                    {
                        "issueIdOrKey": issue_key,
                        "body": {
                            "type": "doc",
                            "version": 1,
                            "content": [{"type": "paragraph", "content": [{"type": "text", "text": f"Merged acme/product PR #{pull['id']}."}]}],
                        },
                    },
                )
                done = call(
                    self,
                    lead,
                    "jira_transition_issue",
                    {"issueIdOrKey": issue_key, "transition": {"id": "31"}},
                )
                self.assertEqual(done["fields"]["status"]["name"], "Done")

            self.assertEqual(merged["state"], "MERGED")
            self.assertEqual(merged["merge_commit"]["hash"], call(
                self,
                lead,
                "bitbucket_get_branch",
                {"workspace": "acme", "repo_slug": "product", "name": "main"},
            )["target"]["hash"])
            operations = system.list_operations(environment.id, limit=100)
            self.assertEqual({item.plugin_id for item in operations}, {"jira", "bitbucket"})
            self.assertEqual({item.actor for item in operations}, {"lead", "engineer"})
            self.assertTrue(any(item.operation == "merge_pull_request" for item in operations))


if __name__ == "__main__":
    unittest.main()
