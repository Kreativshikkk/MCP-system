from __future__ import annotations
from datetime import datetime,timezone
from typing import Any,Callable,Mapping
from uuid import uuid4
from ...errors import ServiceOperationError
from ...plugins import RelationalSession

class LinearError(ServiceOperationError): status_code=400; error="graphql_error"
class LinearNotFound(LinearError): status_code=404; error="not_found"
class LinearForbidden(LinearError): status_code=403; error="forbidden"

class LinearOperations:
    def __init__(self,session:RelationalSession,*,actor:str,now:Callable[[],datetime]|None=None)->None: self.session=session; self.actor=actor; self.now=now or (lambda:datetime.now(timezone.utc))
    def get_viewer(self)->dict[str,Any]: return self._user(self._actor())
    def list_users(self)->dict[str,Any]: return self._connection([self._user(x) for x in self.session.execute("SELECT * FROM linear_users WHERE active ORDER BY name").fetchall()])
    def list_teams(self)->dict[str,Any]: return self._connection([self._team(x) for x in self.session.execute("SELECT * FROM linear_teams ORDER BY key").fetchall()])
    def list_workflow_states(self,team_id:str)->dict[str,Any]: self._team_row(team_id); return self._connection([self._state(x) for x in self.session.execute("SELECT * FROM linear_workflow_states WHERE team_id=? ORDER BY position",(team_id,)).fetchall()])
    def list_issue_labels(self,team_id:str)->dict[str,Any]: self._team_row(team_id); return self._connection([dict(x) for x in self.session.execute("SELECT * FROM linear_labels WHERE team_id=? ORDER BY name",(team_id,)).fetchall()])
    def list_projects(self,team_id:str|None=None)->dict[str,Any]:
        rows=self.session.execute("SELECT * FROM linear_projects"+(" WHERE team_id=?" if team_id else "")+" ORDER BY name",(team_id,) if team_id else ()).fetchall(); return self._connection([self._project(x) for x in rows])
    def get_project(self,id:str)->dict[str,Any]: return self._project(self._project_row(id))
    def create_project(self,team_id:str,name:str,*,description:str|None=None,lead_id:str|None=None)->dict[str,Any]:
        self._team_row(team_id); self._write(); timestamp=self._time(); id=str(uuid4()); self.session.execute("INSERT INTO linear_projects(id,team_id,name,description,state,lead_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",(id,team_id,name,description,"planned",lead_id,timestamp,timestamp)); return {"success":True,"project":self.get_project(id)}
    def update_project(self,id:str,*,name:str|None=None,description:str|None=None,state:str|None=None)->dict[str,Any]:
        row=self._project_row(id); self._write(); self.session.execute("UPDATE linear_projects SET name=?,description=?,state=?,updated_at=? WHERE id=?",(name or row["name"],description if description is not None else row["description"],state or row["state"],self._time(),id)); return {"success":True,"project":self.get_project(id)}
    def list_cycles(self,team_id:str)->dict[str,Any]: self._team_row(team_id); return self._connection([self._cycle(x) for x in self.session.execute("SELECT * FROM linear_cycles WHERE team_id=? ORDER BY number DESC",(team_id,)).fetchall()])
    def create_cycle(self,team_id:str,name:str,*,description:str|None=None,starts_at:str|None=None,ends_at:str|None=None)->dict[str,Any]:
        self._team_row(team_id); self._write(); number=self.session.execute("SELECT count(*) count FROM linear_cycles WHERE team_id=?",(team_id,)).fetchone()["count"]+1; id=str(uuid4()); self.session.execute("INSERT INTO linear_cycles(id,team_id,number,name,description,starts_at,ends_at) VALUES(?,?,?,?,?,?,?)",(id,team_id,number,name,description,starts_at,ends_at)); return {"success":True,"cycle":self._cycle(self._cycle_row(id))}
    def update_cycle(self,id:str,*,name:str|None=None,description:str|None=None,completed_at:str|None=None)->dict[str,Any]:
        row=self._cycle_row(id); self._write(); self.session.execute("UPDATE linear_cycles SET name=?,description=?,completed_at=? WHERE id=?",(name or row["name"],description if description is not None else row["description"],completed_at if completed_at is not None else row["completed_at"],id)); return {"success":True,"cycle":self._cycle(self._cycle_row(id))}
    def list_issues(self,team_id:str,*,state_id:str|None=None,assignee_id:str|None=None)->dict[str,Any]:
        self._team_row(team_id); rows=self.session.execute("SELECT * FROM linear_issues WHERE team_id=? AND archived=0 ORDER BY number DESC",(team_id,)).fetchall(); return self._connection([self._issue(x) for x in rows if (not state_id or x["state_id"]==state_id) and (not assignee_id or x["assignee_id"]==assignee_id)])
    def get_issue(self,id:str)->dict[str,Any]: return self._issue(self._issue_row(id))
    def create_issue(self,team_id:str,title:str,*,description:str|None=None,priority:int=0,state_id:str|None=None,assignee_id:str|None=None,project_id:str|None=None,cycle_id:str|None=None,label_ids:list[str]|None=None)->dict[str,Any]:
        team=self._team_row(team_id); self._write(); state_id=state_id or self.session.execute("SELECT id FROM linear_workflow_states WHERE team_id=? ORDER BY position LIMIT 1",(team_id,)).fetchone()["id"]; number=team["next_issue_number"]; id=str(uuid4()); timestamp=self._time(); self.session.execute("INSERT INTO linear_issues(id,team_id,number,identifier,title,description,priority,state_id,assignee_id,creator_id,project_id,cycle_id,archived,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(id,team_id,number,f"{team['key']}-{number}",title,description,priority,state_id,assignee_id,self._actor()["id"],project_id,cycle_id,False,timestamp,timestamp)); self.session.execute("UPDATE linear_teams SET next_issue_number=next_issue_number+1 WHERE id=?",(team_id,)); [self.session.execute("INSERT INTO linear_issue_labels(issue_id,label_id) VALUES(?,?)",(id,label)) for label in label_ids or []]; return {"success":True,"issue":self.get_issue(id)}
    def update_issue(self,id:str,*,title:str|None=None,description:str|None=None,priority:int|None=None,state_id:str|None=None,assignee_id:str|None=None,project_id:str|None=None,cycle_id:str|None=None)->dict[str,Any]:
        row=self._issue_row(id); self._write()
        if state_id is not None and self.session.execute("SELECT 1 FROM linear_workflow_states WHERE id=? AND team_id=?",(state_id,row["team_id"])).fetchone() is None: raise LinearNotFound("workflow state not found")
        self.session.execute("UPDATE linear_issues SET title=?,description=?,priority=?,state_id=?,assignee_id=?,project_id=?,cycle_id=?,updated_at=? WHERE id=?",(title or row["title"],description if description is not None else row["description"],priority if priority is not None else row["priority"],state_id or row["state_id"],assignee_id if assignee_id is not None else row["assignee_id"],project_id if project_id is not None else row["project_id"],cycle_id if cycle_id is not None else row["cycle_id"],self._time(),id)); return {"success":True,"issue":self.get_issue(id)}
    def archive_issue(self,id:str)->dict[str,Any]: self._issue_row(id); self._write(); self.session.execute("UPDATE linear_issues SET archived=1,updated_at=? WHERE id=?",(self._time(),id)); return {"success":True}
    def list_comments(self,issue_id:str)->dict[str,Any]: self._issue_row(issue_id); return self._connection([self._comment(x) for x in self.session.execute("SELECT * FROM linear_comments WHERE issue_id=? ORDER BY created_at",(issue_id,)).fetchall()])
    def create_comment(self,issue_id:str,body:str)->dict[str,Any]: self._issue_row(issue_id); self._write(); id=str(uuid4()); timestamp=self._time(); self.session.execute("INSERT INTO linear_comments(id,issue_id,user_id,body,created_at,updated_at) VALUES(?,?,?,?,?,?)",(id,issue_id,self._actor()["id"],body,timestamp,timestamp)); return {"success":True,"comment":self._comment(self.session.execute("SELECT * FROM linear_comments WHERE id=?",(id,)).fetchone())}
    def create_issue_relation(self,issue_id:str,related_issue_id:str,type:str)->dict[str,Any]: self._issue_row(issue_id); self._issue_row(related_issue_id); self._write(); id=str(uuid4()); self.session.execute("INSERT INTO linear_issue_relations(id,issue_id,related_issue_id,type,created_at) VALUES(?,?,?,?,?)",(id,issue_id,related_issue_id,type,self._time())); return {"success":True,"issueRelation":{"id":id,"type":type,"issueId":issue_id,"relatedIssueId":related_issue_id}}
    def _actor(self)->Mapping[str,Any]:
        row=self.session.execute("SELECT * FROM linear_users WHERE lower(email)=lower(?) OR lower(name)=lower(?)",(self.actor,self.actor)).fetchone()
        if row is None:
            row=self.session.execute("SELECT * FROM linear_users WHERE lower(email) LIKE lower(?)",(f"{self.actor}@%",)).fetchone()
        if row is None: raise LinearForbidden("unknown actor")
        return row
    def _write(self)->None:
        actor=self._actor();
        if not actor["active"]: raise LinearForbidden("inactive user")
    def _team_row(self,id:str)->Mapping[str,Any]:
        row=self.session.execute("SELECT * FROM linear_teams WHERE id=? OR key=?",(id,id)).fetchone()
        if row is None: raise LinearNotFound("team not found")
        return row
    def _issue_row(self,id:str)->Mapping[str,Any]:
        row=self.session.execute("SELECT * FROM linear_issues WHERE id=? OR identifier=?",(id,id)).fetchone()
        if row is None: raise LinearNotFound("issue not found")
        return row
    def _project_row(self,id:str)->Mapping[str,Any]:
        row=self.session.execute("SELECT * FROM linear_projects WHERE id=?",(id,)).fetchone()
        if row is None: raise LinearNotFound("project not found")
        return row
    def _cycle_row(self,id:str)->Mapping[str,Any]:
        row=self.session.execute("SELECT * FROM linear_cycles WHERE id=?",(id,)).fetchone()
        if row is None: raise LinearNotFound("cycle not found")
        return row
    def _issue(self,row:Mapping[str,Any])->dict[str,Any]:
        state=self.session.execute("SELECT * FROM linear_workflow_states WHERE id=?",(row["state_id"],)).fetchone(); labels=self.session.execute("SELECT l.* FROM linear_issue_labels il JOIN linear_labels l ON l.id=il.label_id WHERE il.issue_id=?",(row["id"],)).fetchall(); return {"id":row["id"],"identifier":row["identifier"],"number":row["number"],"title":row["title"],"description":row["description"],"priority":row["priority"],"state":self._state(state),"assignee":self._user_by_id(row["assignee_id"]),"creator":self._user_by_id(row["creator_id"]),"projectId":row["project_id"],"cycleId":row["cycle_id"],"labels":{"nodes":[dict(x) for x in labels]},"archivedAt":row["updated_at"] if row["archived"] else None,"createdAt":str(row["created_at"]),"updatedAt":str(row["updated_at"])}
    def _user_by_id(self,id:str|None)->dict[str,Any]|None: return self._user(self.session.execute("SELECT * FROM linear_users WHERE id=?",(id,)).fetchone()) if id else None
    @staticmethod
    def _connection(nodes:list[Any])->dict[str,Any]: return {"nodes":nodes,"pageInfo":{"hasNextPage":False,"hasPreviousPage":False}}
    @staticmethod
    def _user(x:Mapping[str,Any])->dict[str,Any]: return {"id":x["id"],"email":x["email"],"name":x["name"],"displayName":x["display_name"],"active":bool(x["active"]),"admin":bool(x["admin"])}
    @staticmethod
    def _team(x:Mapping[str,Any])->dict[str,Any]: return {"id":x["id"],"key":x["key"],"name":x["name"],"description":x["description"]}
    @staticmethod
    def _state(x:Mapping[str,Any])->dict[str,Any]: return {"id":x["id"],"name":x["name"],"type":x["type"],"position":x["position"]}
    @staticmethod
    def _project(x:Mapping[str,Any])->dict[str,Any]: return {"id":x["id"],"name":x["name"],"description":x["description"],"state":x["state"],"teamId":x["team_id"],"leadId":x["lead_id"]}
    @staticmethod
    def _cycle(x:Mapping[str,Any])->dict[str,Any]: return {"id":x["id"],"number":x["number"],"name":x["name"],"description":x["description"],"startsAt":str(x["starts_at"]) if x["starts_at"] else None,"endsAt":str(x["ends_at"]) if x["ends_at"] else None,"completedAt":str(x["completed_at"]) if x["completed_at"] else None}
    def _comment(self,x:Mapping[str,Any])->dict[str,Any]: return {"id":x["id"],"body":x["body"],"user":self._user_by_id(x["user_id"]),"createdAt":str(x["created_at"]),"updatedAt":str(x["updated_at"])}
    def _time(self)->str:return self.now().isoformat()
