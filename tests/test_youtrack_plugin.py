import json,tempfile,unittest
from pathlib import Path
from mcp_system import MCPSystem,PluginRegistry
from mcp_system.config import load_template_spec
from mcp_system.http import HTTPRequest,InspectorHTTPRouter
from mcp_system.mcp import MCPDispatcher
from mcp_system.service_plugins import YouTrackPlugin
class YouTrackPluginTests(unittest.TestCase):
 def test_invalid_command_and_unknown_actor_are_tool_errors(self):
  with tempfile.TemporaryDirectory() as root:
   r=PluginRegistry();r.register(YouTrackPlugin());s=MCPSystem(Path(root),r);x=load_template_spec(Path("configs/templates/youtrack-default.toml"));s.create_template(x);e=s.create_environment_from_template(x.template_id);lead=MCPDispatcher(s,e.id,actor="lead");issue=lead.call_tool("youtrack_create_issue",{"projectId":"PROD","summary":"Validate command"})["structuredContent"]["result"]
   invalid=lead.call_tool("youtrack_apply_command",{"issueId":issue["idReadable"],"query":"teleport moon"});unknown=MCPDispatcher(s,e.id,actor="intruder").call_tool("youtrack_get_current_user",{});missing_agile=lead.call_tool("youtrack_create_sprint",{"agileId":"missing","name":"Sprint"});missing_sprint=lead.call_tool("youtrack_update_sprint",{"agileId":"agile-1","sprintId":"missing"})
   self.assertTrue(invalid["isError"]);self.assertTrue(unknown["isError"]);self.assertEqual(missing_agile["structuredContent"]["error"]["status"],404);self.assertEqual(missing_sprint["structuredContent"]["error"]["status"],404)
 def test_issue_command_comment_link_sprint_and_work_item(self):
  with tempfile.TemporaryDirectory() as root:
   r=PluginRegistry();r.register(YouTrackPlugin());s=MCPSystem(Path(root),r);x=load_template_spec(Path("configs/templates/youtrack-default.toml"));s.create_template(x);e=s.create_environment_from_template(x.template_id);d=MCPDispatcher(s,e.id,actor="lead");self.assertEqual(len(d.list_tools()),23)
   def call(n,a):
    z=d.call_tool(n,a);self.assertFalse(z["isError"],z["structuredContent"]);return z["structuredContent"]["result"]
   one=call("youtrack_create_issue",{"projectId":"PROD","summary":"Refresh race","assignee":"engineer"});two=call("youtrack_create_issue",{"projectId":"PROD","summary":"Regression coverage"});call("youtrack_apply_command",{"issueId":one["idReadable"],"query":"In Progress"});call("youtrack_create_comment",{"issueId":one["idReadable"],"text":"Started."});call("youtrack_create_link",{"issueId":two["idReadable"],"relatedIssueId":one["idReadable"],"linkType":"depends on"});sp=call("youtrack_create_sprint",{"agileId":"agile-1","name":"Sprint 1"});call("youtrack_create_work_item",{"issueId":one["idReadable"],"durationMinutes":30,"text":"Investigation"});self.assertEqual(sp["name"],"Sprint 1")
   tag=call("youtrack_create_tag",{"projectId":"PROD","name":"needs-triage"});one=call("youtrack_set_issue_tags",{"issueId":one["idReadable"],"tagIds":[tag["id"]]});self.assertEqual(one["tags"][0]["name"],"needs-triage")
   p=json.loads(InspectorHTTPRouter(s).dispatch(HTTPRequest("GET",f"/api/environments/{e.id}/workbench")).body)["services"][0]["projection"];self.assertEqual(p["provider"]["id"],"youtrack");self.assertEqual(len(p["repositories"][0]["tickets"]),2)
