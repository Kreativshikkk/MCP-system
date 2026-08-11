from __future__ import annotations
import json
from pathlib import Path
import unittest
from mcp_system.mcp.linear_surface import linear_graphql_surface


class LinearContractTests(unittest.TestCase):
    def test_graphql_selection_is_pinned_and_bounded(self) -> None:
        provenance = json.loads(Path("contracts/linear/provenance.json").read_text())
        selected = json.loads(Path("contracts/linear/selected-operations.json").read_text())
        self.assertEqual(selected["sourceSha256"], provenance["sourceSha256"])
        self.assertEqual(len(selected["operations"]), 20)
        self.assertEqual({item["kind"] for item in selected["operations"]}, {"query", "mutation"})
        self.assertEqual(len({item["mcpTool"] for item in selected["operations"]}), 20)
        self.assertEqual({tool.name for tool in linear_graphql_surface().tools}, {item["mcpTool"] for item in selected["operations"]})
        tools = {tool.name: tool for tool in linear_graphql_surface().tools}
        for operation in selected["operations"]:
            with self.subTest(field=operation["field"]):
                tool = tools[operation["mcpTool"]]
                self.assertEqual(
                    set(tool.input_schema.get("required", ())),
                    set(operation["required"]),
                )
                self.assertEqual(
                    tool.read_only,
                    operation["kind"] == "query",
                )
                self.assertIn("Linear GraphQL API", tool.description)

if __name__ == "__main__": unittest.main()
