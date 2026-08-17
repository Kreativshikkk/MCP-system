import json,unittest
from pathlib import Path
from mcp_system.mcp.youtrack_surface import youtrack_rest_surface
class YouTrackContractTests(unittest.TestCase):
 def test_selection_is_pinned(self):
  p=json.loads(Path("contracts/youtrack/provenance.json").read_text());s=json.loads(Path("contracts/youtrack/selected-operations.json").read_text());self.assertEqual(s["sourceSha256"],p["sourceSha256"]);self.assertEqual(len(s["operations"]),23);self.assertEqual(len({x["mcpTool"] for x in s["operations"]}),23)
  self.assertEqual({x.name for x in youtrack_rest_surface().tools},{x["mcpTool"] for x in s["operations"]})
  tools={x.name:x for x in youtrack_rest_surface().tools}
  required={
   "get_current_user":set(),"list_users":set(),"list_projects":set(),"get_project":{"projectId"},"list_issues":set(),
   "create_issue":{"projectId","summary"},"get_issue":{"issueId"},"update_issue":{"issueId"},"apply_command":{"issueId","query"},
   "list_comments":{"issueId"},"create_comment":{"issueId","text"},"list_links":{"issueId"},"create_link":{"issueId","relatedIssueId","linkType"},
   "list_agiles":set(),"list_sprints":{"agileId"},"create_sprint":{"agileId","name"},"update_sprint":{"agileId","sprintId"},
   "list_work_items":{"issueId"},"create_work_item":{"issueId","durationMinutes"},"list_vcs_changes":{"issueId"},
   "list_tags":{"projectId"},"create_tag":{"projectId","name"},"set_issue_tags":{"issueId","tagIds"}}
  for operation in s["operations"]:
   tool=tools[operation["mcpTool"]]
   self.assertEqual(set(tool.input_schema.get("required",())),required[operation["localOperation"]])
   self.assertEqual(tool.read_only,operation["method"]=="GET")
