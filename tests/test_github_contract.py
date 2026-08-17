from __future__ import annotations

import json
from pathlib import Path
import unittest

from mcp_system.http.github import API_VERSION, ROUTES
from mcp_system.mcp.github_surface import github_rest_v3_surface
from mcp_system.service_plugins import GitHubPlugin


CONTRACT = Path("contracts/github/core-openapi.json")
SOURCE = Path("contracts/github/openapi-source.json")


class GitHubOpenAPIContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        cls.source = json.loads(SOURCE.read_text(encoding="utf-8"))
        cls.operations = {
            (operation["method"], operation["path"]): operation
            for operation in cls.contract["operations"]
        }

    def test_every_http_route_matches_the_pinned_official_operation(self) -> None:
        self.assertEqual(self.contract["source_revision"], self.source["source_revision"])
        self.assertEqual(self.contract["source_document"], self.source["source_document"])
        self.assertEqual(self.contract["source_document_sha256"], self.source["source_document_sha256"])
        self.assertEqual(self.contract["api_version"], API_VERSION)
        self.assertEqual(GitHubPlugin().manifest.contract_revision, self.source["source_revision"])
        self.assertEqual(len(ROUTES), len(self.operations))
        for route in ROUTES:
            with self.subTest(method=route.method, path=route.path):
                operation = self.operations[(route.method, route.path)]
                if route.operation == "is_merged":
                    self.assertLessEqual({"204", "404"}, set(operation["responses"]))
                else:
                    self.assertIn(str(route.status), operation["responses"])
                request = operation["request"]
                self.assertLessEqual(set(route.body_fields), set(request["properties"]))
                self.assertLessEqual(set(request["required"]), set(route.required_body))
                alternatives = request["any_of_required"]
                if alternatives:
                    self.assertTrue(any(set(choice) <= set(route.required_body) for choice in alternatives))
                self.assertLessEqual(set(route.query_fields), set(operation["query"]["properties"]))

    def test_public_merge_surface_uses_github_request_semantics(self) -> None:
        tools = {tool.operation: tool for tool in github_rest_v3_surface().tools}
        merge = tools["merge_pull_request_api"].input_schema
        self.assertNotIn("merge_commit_sha", merge["properties"])
        self.assertLessEqual(
            {"commit_title", "commit_message", "sha", "merge_method"},
            set(merge["properties"]),
        )

    def test_actions_surface_covers_run_job_discovery_and_logs(self) -> None:
        tools = {tool.operation: tool for tool in github_rest_v3_surface().tools}
        self.assertLessEqual(
            {"list_workflow_runs", "list_workflow_jobs", "get_workflow_job", "get_workflow_job_log"},
            set(tools),
        )
        for operation in ("list_workflow_runs", "list_workflow_jobs", "get_workflow_job", "get_workflow_job_log"):
            self.assertTrue(tools[operation].read_only)
        self.assertFalse(tools["dispatch_workflow"].read_only)
        self.assertNotIn("complete_workflow_run", tools)

    def test_git_refs_surface_matches_github_vocabulary(self) -> None:
        tools = {tool.operation: tool for tool in github_rest_v3_surface().tools}
        self.assertLessEqual(
            {"create_ref", "get_ref", "list_matching_refs", "update_ref"},
            set(tools),
        )
        self.assertEqual(
            tools["update_ref"].input_schema["required"],
            ["owner", "repo", "ref", "sha"],
        )

    def test_release_selection_is_exposed(self) -> None:
        tools = {tool.operation: tool for tool in github_rest_v3_surface().tools}
        self.assertTrue(tools["list_releases"].read_only)
        self.assertFalse(tools["create_release"].read_only)


if __name__ == "__main__":
    unittest.main()
