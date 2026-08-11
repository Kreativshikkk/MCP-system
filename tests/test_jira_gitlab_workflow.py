from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from mcp_system import MCPSystem, PluginRegistry, TemplateSpec
from mcp_system.config import load_template_spec
from mcp_system.service_plugins import GitLabPlugin, JiraPlugin


class JiraGitLabWorkflowTests(unittest.TestCase):
    def test_ticket_branch_merge_ci_and_done_trace(self) -> None:
        jira = load_template_spec(Path("configs/templates/jira-default.toml"))
        gitlab = load_template_spec(Path("configs/templates/gitlab-default.toml"))
        company = TemplateSpec(
            template_id="jira_gitlab_company", name="Jira and GitLab company",
            version="1.0.0", services=(*jira.services, *gitlab.services),
            mcp_surfaces=("jira_rest_v3", "gitlab_rest_v4"),
        )
        with tempfile.TemporaryDirectory() as root:
            registry = PluginRegistry(); registry.register(JiraPlugin()); registry.register(GitLabPlugin())
            system = MCPSystem(Path(root), registry)
            system.create_template(company)
            environment = system.create_environment_from_template(company.template_id)

            ticket = system.invoke_service_operation(environment.id, "jira", actor="lead", transport="mcp", operation="create_issue", arguments={"fields": {"project": {"key": "PROD"}, "summary": "Fix refresh race", "issuetype": {"name": "Bug"}, "assignee": {"accountId": "engineer"}, "labels": ["bug"]}})
            system.invoke_service_operation(environment.id, "jira", actor="engineer", transport="mcp", operation="transition_issue", arguments={"issue_id_or_key": ticket["key"], "transition": {"id": "21"}})
            base = system.invoke_service_operation(environment.id, "gitlab", actor="engineer", transport="mcp", operation="create_commit", arguments={"project": "acme/product", "message": "Initial commit", "author": "engineer", "files": {"app.py": "SAFE = False\n"}})
            system.invoke_service_operation(environment.id, "gitlab", actor="engineer", transport="mcp", operation="create_branch", arguments={"project": "acme/product", "branch": "main", "ref": base["sha"]})
            head = system.invoke_service_operation(environment.id, "gitlab", actor="engineer", transport="mcp", operation="create_commit", arguments={"project": "acme/product", "message": f"{ticket['key']} fix refresh race", "author": "engineer", "parent_shas": [base["sha"]], "files": {"app.py": "SAFE = True\n"}})
            system.invoke_service_operation(environment.id, "gitlab", actor="engineer", transport="mcp", operation="create_branch", arguments={"project": "acme/product", "branch": f"fix/{ticket['key'].lower()}", "ref": head["sha"]})
            merge_request = system.invoke_service_operation(environment.id, "gitlab", actor="engineer", transport="mcp", operation="create_merge_request", arguments={"project": "acme/product", "title": f"{ticket['key']} Fix refresh race", "source_branch": f"fix/{ticket['key'].lower()}", "target_branch": "main", "reviewers": ["qa"]})
            system.invoke_service_operation(environment.id, "gitlab", actor="qa", transport="mcp", operation="approve_merge_request", arguments={"project": "acme/product", "merge_request_iid": merge_request["iid"]})
            pipeline = system.invoke_service_operation(environment.id, "gitlab", actor="engineer", transport="mcp", operation="create_pipeline", arguments={"project": "acme/product", "ref": f"fix/{ticket['key'].lower()}"})
            system.invoke_service_operation(environment.id, "gitlab", actor="engineer", transport="mcp", operation="update_pipeline", arguments={"project": "acme/product", "pipeline_id": pipeline["id"], "status": "success"})
            system.invoke_service_operation(environment.id, "gitlab", actor="lead", transport="mcp", operation="merge_merge_request", arguments={"project": "acme/product", "merge_request_iid": merge_request["iid"], "sha": head["sha"]})
            done = system.invoke_service_operation(environment.id, "jira", actor="lead", transport="mcp", operation="transition_issue", arguments={"issue_id_or_key": ticket["key"], "transition": {"id": "31"}})

            self.assertEqual(done["fields"]["status"]["name"], "Done")
            operations = system.list_operations(environment.id, limit=100)
            self.assertEqual({operation.plugin_id for operation in operations}, {"jira", "gitlab"})
            self.assertTrue(any(operation.operation == "merge_merge_request" for operation in operations))


if __name__ == "__main__":
    unittest.main()
