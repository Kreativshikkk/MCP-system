from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

from mcp_system.mcp.jira_surface import jira_rest_v3_surface
from mcp_system.service_plugins import JiraPlugin


ROOT = Path("contracts/jira")


class JiraContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.provenance = json.loads((ROOT / "provenance.json").read_text())
        cls.selection = json.loads((ROOT / "selected-operations.json").read_text())
        cls.documents = {
            "platform": json.loads((ROOT / "platform-openapi-1.8516.75.json").read_text()),
            "software": json.loads((ROOT / "software-openapi-1.8516.75.json").read_text()),
        }

    def test_pinned_documents_match_recorded_checksums(self) -> None:
        for document in self.provenance["documents"]:
            payload = (ROOT / document["file"]).read_bytes()
            self.assertEqual(hashlib.sha256(payload).hexdigest(), document["sha256"])
        self.assertEqual(JiraPlugin().manifest.contract_revision, self.provenance["upstream_version"])

    def test_every_selected_operation_exists_with_recorded_status_codes(self) -> None:
        for selected in self.selection["operations"]:
            operation = self.documents[selected["api"]]["paths"][selected["path"]][selected["method"]]
            self.assertEqual(operation["operationId"], selected["operationId"])
            actual = {int(code) for code in operation["responses"] if code.isdigit()}
            self.assertTrue(set(selected["success"]).issubset(actual), selected["operationId"])
            self.assertTrue(set(selected["errors"]).issubset(actual), selected["operationId"])

    def test_surface_is_exactly_the_selected_bounded_adapter(self) -> None:
        surface = jira_rest_v3_surface()
        tools = {tool.name: tool for tool in surface.tools}
        selected = {item["tool"]: item for item in self.selection["operations"]}
        self.assertEqual(set(tools), set(selected))
        for name, item in selected.items():
            self.assertEqual(tools[name].operation, item["local"])
            self.assertEqual(tools[name].read_only, item["readOnly"])
            self.assertIn("Jira", tools[name].description)

    def test_mutation_tools_use_provider_request_vocabulary(self) -> None:
        tools = {tool.name: tool for tool in jira_rest_v3_surface().tools}
        expected_properties = {
            "jira_create_issue": {"fields", "update", "properties", "transition", "historyMetadata"},
            "jira_update_issue": {"issueIdOrKey", "fields", "update", "properties", "transition", "historyMetadata"},
            "jira_transition_issue": {"issueIdOrKey", "transition", "fields", "update", "properties", "historyMetadata"},
            "jira_add_comment": {"issueIdOrKey", "body", "visibility", "properties"},
            "jira_create_issue_link": {"type", "outwardIssue", "inwardIssue", "comment"},
            "jira_create_sprint": {"name", "goal", "startDate", "endDate", "originBoardId"},
            "jira_update_sprint": {"sprintId", "name", "goal", "startDate", "endDate", "state"},
            "jira_move_issues_to_sprint": {"sprintId", "issues", "rankAfterIssue", "rankBeforeIssue", "rankCustomFieldId"},
        }
        for name, properties in expected_properties.items():
            self.assertEqual(set(tools[name].input_schema["properties"]), properties, name)

    def test_search_and_pagination_use_jira_names(self) -> None:
        tools = {tool.name: tool for tool in jira_rest_v3_surface().tools}
        self.assertTrue({"jql", "maxResults", "nextPageToken", "fields"}.issubset(tools["jira_search_issues"].input_schema["properties"]))
        self.assertTrue({"startAt", "maxResults"}.issubset(tools["jira_list_users"].input_schema["properties"]))
        self.assertTrue({"startAt", "maxResults", "projectKeyOrId"}.issubset(tools["jira_list_boards"].input_schema["properties"]))


if __name__ == "__main__":
    unittest.main()
