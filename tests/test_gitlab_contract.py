from __future__ import annotations

import json
from pathlib import Path
import re
import unittest

from mcp_system.http.gitlab import OPENAPI_REVISION, ROUTES
from mcp_system.mcp.gitlab_surface import gitlab_rest_v4_surface
from mcp_system.service_plugins import GitLabPlugin


CONTRACT = Path("contracts/gitlab-core-openapi.json")

# Observed against the pinned local GitLab CE runtime. Keep exceptions narrow:
# the OpenAPI source says 200, while GitLab CE 19.2 actually returns 204.
RUNTIME_STATUS_DEVIATIONS = {
    ("DELETE", "/api/v4/projects/{}/labels"): "204",
}


def normalized(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


class GitLabOpenAPIContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(CONTRACT.read_text())
        cls.operations = {
            (operation["method"], normalized(operation["path"])): operation
            for operation in cls.contract["operations"]
        }

    def test_every_http_route_is_pinned_to_official_openapi_operation(self) -> None:
        self.assertEqual(self.contract["revision"], OPENAPI_REVISION)
        self.assertEqual(GitLabPlugin().manifest.contract_revision, OPENAPI_REVISION)
        self.assertEqual(self.contract["gitlab_version"], "19.3.0-pre")
        self.assertEqual(len(ROUTES), len(self.operations))
        for route in ROUTES:
            key = (route.method, normalized(f"/api/v4{route.path}"))
            with self.subTest(method=route.method, path=route.path):
                operation = self.operations[key]
                expected_status = RUNTIME_STATUS_DEVIATIONS.get(key)
                if expected_status is None:
                    self.assertIn(str(route.status), operation["responses"])
                else:
                    self.assertEqual(str(route.status), expected_status)
                json_schema = operation["request"].get("application/json")
                if json_schema:
                    self.assertLessEqual(set(json_schema["required"]), set(route.required_body))
                    self.assertLessEqual(set(route.body_fields), set(json_schema["properties"]))

    def test_public_mcp_surface_uses_gitlab_commit_and_pipeline_semantics(self) -> None:
        tools = {tool.operation: tool for tool in gitlab_rest_v4_surface().tools}
        commit = tools["create_repository_commit"].input_schema
        self.assertEqual(
            commit["required"],
            ["project", "branch", "commit_message", "actions"],
        )
        self.assertNotIn("create_commit", tools)
        self.assertNotIn("update_pipeline", tools)
        # an agent that can write a CI verdict can forge a green build
        self.assertNotIn("set_commit_status", tools)
        self.assertNotIn("set_branch_head", tools)
        self.assertIn("start_branch", commit["properties"])
        self.assertIn("start_sha", commit["properties"])
        self.assertEqual(
            tools["create_pipeline"].input_schema["required"],
            ["project", "ref"],
        )


if __name__ == "__main__":
    unittest.main()
