from __future__ import annotations
from dataclasses import dataclass
from typing import Any,Mapping
from ...git_storage import GitServiceDataPlane
from ...plugins import RelationalSession
@dataclass(frozen=True,slots=True)
class LinearInspectorAdapter:
    plugin_id:str="linear"; plugin_version:str="0.1.0"
    def project(self,session:RelationalSession,git_data_plane:GitServiceDataPlane|None)->Mapping[str,Any]:
        return {"provider":{"id":"linear","name":"Linear"},"repositories":[self._team(session,x) for x in session.execute("SELECT * FROM linear_teams ORDER BY key").fetchall()]}
    def _team(self,s:RelationalSession,team:Mapping[str,Any])->dict[str,Any]:
        tickets=[]
        for i in s.execute("SELECT i.*,u.name creator,st.name state_name,st.type state_type FROM linear_issues i JOIN linear_users u ON u.id=i.creator_id JOIN linear_workflow_states st ON st.id=i.state_id WHERE i.team_id=? AND i.archived=0 ORDER BY i.number DESC",(team["id"],)).fetchall():
            assignee=s.execute("SELECT name FROM linear_users WHERE id=?",(i["assignee_id"],)).fetchone() if i["assignee_id"] else None; labels=s.execute("SELECT l.name FROM linear_issue_labels il JOIN linear_labels l ON l.id=il.label_id WHERE il.issue_id=?",(i["id"],)).fetchall(); comments=s.execute("SELECT c.*,u.name author FROM linear_comments c JOIN linear_users u ON u.id=c.user_id WHERE c.issue_id=? ORDER BY c.created_at",(i["id"],)).fetchall(); relations=s.execute("SELECT r.*,other.identifier related FROM linear_issue_relations r JOIN linear_issues other ON other.id=r.related_issue_id WHERE r.issue_id=?",(i["id"],)).fetchall()
            tickets.append({"id":i["id"],"number":i["number"],"title":f"{i['identifier']} · {i['title']}","description":i["description"],"state":"closed" if i["state_type"] in {"completed","canceled"} else "open","stateReason":i["state_name"],"author":i["creator"],"labels":[x["name"] for x in labels],"assignees":[assignee["name"]] if assignee else [],"comments":[{"id":x["id"],"author":x["author"],"body":x["body"],"createdAt":_time(x["created_at"])} for x in comments],"iterations":[],"links":[{"id":x["id"],"type":x["type"],"direction":"outward","issueKey":x["related"]} for x in relations],"createdAt":_time(i["created_at"]),"updatedAt":_time(i["updated_at"])})
        return {"id":team["id"],"provider":"linear","name":team["name"],"fullName":team["key"],"visibility":"private","archived":False,"defaultBranch":None,"tickets":tickets,"changeSets":[],"builds":[]}
def _time(v:Any)->Any:return v.isoformat() if hasattr(v,"isoformat") else v
