from __future__ import annotations
import json,tempfile,unittest
from pathlib import Path
from mcp_system import MCPSystem,PluginRegistry
from mcp_system.config import load_template_spec
from mcp_system.http import HTTPRequest,InspectorHTTPRouter
from mcp_system.mcp import MCPDispatcher
from mcp_system.service_plugins import LinearPlugin
class LinearPluginTests(unittest.TestCase):
 def test_invalid_state_and_unknown_actor_are_tool_errors(self):
  with tempfile.TemporaryDirectory() as root:
   r=PluginRegistry();r.register(LinearPlugin());s=MCPSystem(Path(root),r);spec=load_template_spec(Path("configs/templates/linear-default.toml"));s.create_template(spec);e=s.create_environment_from_template(spec.template_id);lead=MCPDispatcher(s,e.id,actor="lead")
   issue=lead.call_tool("linear_create_issue",{"teamId":"team-1","title":"Validate state"})["structuredContent"]["result"]["issue"]
   invalid=lead.call_tool("linear_update_issue",{"id":issue["id"],"stateId":"missing-state"});unknown=MCPDispatcher(s,e.id,actor="intruder").call_tool("linear_get_viewer",{})
   self.assertTrue(invalid["isError"]);self.assertTrue(unknown["isError"])

 def test_project_cycle_issue_comment_relation_and_inspector(self)->None:
  with tempfile.TemporaryDirectory() as root:
   r=PluginRegistry();r.register(LinearPlugin());s=MCPSystem(Path(root),r);spec=load_template_spec(Path("configs/templates/linear-default.toml"));s.create_template(spec);e=s.create_environment_from_template(spec.template_id);d=MCPDispatcher(s,e.id,actor="lead")
   self.assertEqual(len(d.list_tools()),22)
   def call(name,args):
    x=d.call_tool(name,args);self.assertFalse(x["isError"],x["structuredContent"]);return x["structuredContent"]["result"]
   users=call("linear_list_users",{})["nodes"];engineer=next(x for x in users if x["name"]=="engineer");states=call("linear_list_workflow_states",{"teamId":"team-1"})["nodes"]
   project=call("linear_create_project",{"teamId":"team-1","name":"Launch"})["project"];cycle=call("linear_create_cycle",{"teamId":"team-1","name":"Cycle 1"})["cycle"]
   first=call("linear_create_issue",{"teamId":"team-1","title":"Refresh race","assigneeId":engineer["id"],"projectId":project["id"],"cycleId":cycle["id"]})["issue"]
   label=call("linear_create_issue_label",{"teamId":"team-1","name":"Needs Triage","color":"#f59e0b"})["issueLabel"]
   first=call("linear_set_issue_labels",{"issueId":first["id"],"labelIds":[label["id"]]})["issue"]
   self.assertEqual([x["name"] for x in first["labels"]["nodes"]],["Needs Triage"])
   second=call("linear_create_issue",{"teamId":"team-1","title":"Add regression coverage"})["issue"]
   call("linear_update_issue",{"id":first["id"],"stateId":next(x["id"] for x in states if x["name"]=="In Progress")});call("linear_create_comment",{"issueId":first["id"],"body":"Implementation started."});call("linear_create_issue_relation",{"issueId":second["id"],"relatedIssueId":first["id"],"type":"blocks"})
   response=InspectorHTTPRouter(s).dispatch(HTTPRequest("GET",f"/api/environments/{e.id}/workbench"));p=json.loads(response.body)["services"][0]["projection"];self.assertEqual(p["provider"]["id"],"linear");self.assertEqual(len(p["repositories"][0]["tickets"]),2)
if __name__=="__main__":unittest.main()
