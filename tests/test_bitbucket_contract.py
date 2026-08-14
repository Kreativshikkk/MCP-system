from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

from mcp_system.mcp.bitbucket_surface import bitbucket_cloud_v2_surface
from mcp_system.service_plugins import BitbucketPlugin


# Implemented by the replica, never exposed to an agent (see update_pipeline).
ADMIN_ONLY_OPERATIONS = {"bitbucket_create_commit_status"}


class BitbucketContractTests(unittest.TestCase):
    def test_selected_operations_are_pinned_and_workflow_bounded(self) -> None:
        provenance = json.loads(Path("contracts/bitbucket/provenance.json").read_text())
        selected_path = Path("contracts/bitbucket/selected-operations.json")
        selected = json.loads(selected_path.read_text())
        self.assertEqual(selected["apiVersion"], "2.0")
        self.assertEqual(selected["sourceSha256"], provenance["sourceSha256"])
        self.assertEqual(selected["operationCount"], 36)
        self.assertEqual(
            {item["workflow"] for item in selected["operations"]},
            {"identity", "membership", "repository", "source", "branch", "ticket", "review", "ci"},
        )
        self.assertEqual(
            len({item["mcpTool"] for item in selected["operations"]}), 36
        )
        self.assertEqual(
            hashlib.sha256(selected_path.read_bytes()).hexdigest(),
            "5a513a4a776112741f3e60657920f96ac817ebe48a0a27d46baaef3ade2a4888",
        )
        surface = bitbucket_cloud_v2_surface()
        self.assertEqual(surface.plugin_id, BitbucketPlugin().manifest.plugin_id)
        # The replica implements every selected operation; the agent-facing
        # surface deliberately withholds the CI verdict writers, because an
        # agent that can post SUCCESSFUL can forge a green build.
        self.assertEqual(
            {tool.name for tool in surface.tools} | ADMIN_ONLY_OPERATIONS,
            {item["mcpTool"] for item in selected["operations"]},
        )
        self.assertFalse(
            {tool.name for tool in surface.tools} & ADMIN_ONLY_OPERATIONS
        )
        tools = {tool.name: tool for tool in surface.tools}
        response_documentation_gaps = {
            "list_issue_comments": ["200"],
            "list_pipelines": ["200"],
            "list_pipeline_steps": ["200"],
        }
        redirect_operations = {"get_pull_request_diff": ["302"]}
        for operation in selected["operations"]:
            with self.subTest(operation=operation["localOperation"]):
                if operation["localOperation"] in redirect_operations:
                    self.assertEqual(
                        operation["responses"],
                        redirect_operations[operation["localOperation"]],
                    )
                else:
                    self.assertTrue(operation["successResponses"])
                if not operation["errorResponses"] and operation["localOperation"] not in redirect_operations:
                    self.assertEqual(
                        operation["responses"],
                        response_documentation_gaps[operation["localOperation"]],
                    )
                if operation["mcpTool"] in ADMIN_ONLY_OPERATIONS:
                    continue  # implemented, but never on the agent surface
                tool = tools[operation["mcpTool"]]
                contract_path_fields = {
                    item["name"]
                    for item in operation["parameters"]
                    if item["in"] == "path" and item["required"]
                }
                self.assertLessEqual(
                    contract_path_fields,
                    set(tool.input_schema.get("required", ())),
                )
                self.assertLessEqual(
                    contract_path_fields,
                    set(tool.input_schema["properties"]),
                )


if __name__ == "__main__":
    unittest.main()
