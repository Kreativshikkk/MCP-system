from datetime import datetime,timezone
from uuid import uuid4
from ...errors import ServiceOperationError
class YTError(ServiceOperationError):status_code=400;error="youtrack_error"
class YTNotFound(YTError):status_code=404;error="not_found"
class YouTrackOperations:
 def __init__(self,s,*,actor,now=None):self.s=s;self.actor=actor;self.now=now or (lambda:datetime.now(timezone.utc))
 def get_current_user(self):return self._user(self._actor())
 def list_users(self):return [self._user(x) for x in self.s.execute("SELECT * FROM yt_users ORDER BY login").fetchall()]
 def list_projects(self):return [self._project(x) for x in self.s.execute("SELECT * FROM yt_projects ORDER BY short_name").fetchall()]
 def get_project(self,project_id):return self._project(self._project_row(project_id))
 def list_issues(self,*,query=None):
  rows=self.s.execute("SELECT * FROM yt_issues ORDER BY created_at DESC").fetchall();return [self._issue(x) for x in rows if not query or query.lower() in (x["summary"]+" "+x["id_readable"]+" "+x["state"]).lower()]
 def create_issue(self,project_id,summary,*,description=None,assignee=None,priority="Normal"):
  p=self._project_row(project_id);num=p["next_issue"];id=str(uuid4());t=self._time();a=self._user_row(assignee) if assignee else None;self.s.execute("INSERT INTO yt_issues(id,id_readable,project_id,summary,description,state,priority,reporter_id,assignee_id,created_at,updated_at) VALUES(?,?,?,?,?,'Open',?,?,?,?,?)",(id,f"{p['short_name']}-{num}",p["id"],summary,description,priority,self._actor()["id"],a["id"] if a else None,t,t));self.s.execute("UPDATE yt_projects SET next_issue=next_issue+1 WHERE id=?",(p["id"],));return self.get_issue(id)
 def get_issue(self,issue_id):return self._issue(self._issue_row(issue_id))
 def update_issue(self,issue_id,*,summary=None,description=None,assignee=None,priority=None):
  i=self._issue_row(issue_id);aid=self._user_row(assignee)["id"] if assignee else i["assignee_id"];self.s.execute("UPDATE yt_issues SET summary=?,description=?,assignee_id=?,priority=?,updated_at=? WHERE id=?",(summary or i["summary"],description if description is not None else i["description"],aid,priority or i["priority"],self._time(),i["id"]));return self.get_issue(issue_id)
 def apply_command(self,issue_id,query):
  i=self._issue_row(issue_id);q=query.lower()
  if "in progress" in q:state="In Progress"
  elif "fixed" in q or "done" in q:state="Fixed"
  elif q.strip() in {"open","reopen"}:state="Open"
  else:raise YTError("unsupported command")
  self.s.execute("UPDATE yt_issues SET state=?,updated_at=? WHERE id=?",(state,self._time(),i["id"]));return {"issues":[self.get_issue(issue_id)],"query":query}
 def list_comments(self,issue_id):i=self._issue_row(issue_id);return [self._comment(x) for x in self.s.execute("SELECT * FROM yt_comments WHERE issue_id=? ORDER BY id",(i["id"],)).fetchall()]
 def create_comment(self,issue_id,text):i=self._issue_row(issue_id);r=self.s.execute("INSERT INTO yt_comments(issue_id,author_id,text,created_at) VALUES(?,?,?,?) RETURNING id",(i["id"],self._actor()["id"],text,self._time())).fetchone();return self._comment(self.s.execute("SELECT * FROM yt_comments WHERE id=?",(r["id"],)).fetchone())
 def list_links(self,issue_id):i=self._issue_row(issue_id);return [dict(x) for x in self.s.execute("SELECT * FROM yt_links WHERE issue_id=?",(i["id"],)).fetchall()]
 def create_link(self,issue_id,related_issue_id,link_type):a=self._issue_row(issue_id);b=self._issue_row(related_issue_id);r=self.s.execute("INSERT INTO yt_links(issue_id,related_issue_id,link_type) VALUES(?,?,?) RETURNING id",(a["id"],b["id"],link_type)).fetchone();return {"id":r["id"],"linkType":link_type,"issue":self.get_issue(related_issue_id)}
 def list_agiles(self):return [dict(x) for x in self.s.execute("SELECT * FROM yt_agiles ORDER BY name").fetchall()]
 def list_sprints(self,agile_id):self._agile_row(agile_id);return [self._sprint(x) for x in self.s.execute("SELECT * FROM yt_sprints WHERE agile_id=? ORDER BY start_at",(agile_id,)).fetchall()]
 def create_sprint(self,agile_id,name,*,goal=None,start_at=None,finish_at=None):self._agile_row(agile_id);id=str(uuid4());self.s.execute("INSERT INTO yt_sprints(id,agile_id,name,goal,archived,start_at,finish_at) VALUES(?,?,?,?,0,?,?)",(id,agile_id,name,goal,start_at,finish_at));return self._sprint(self.s.execute("SELECT * FROM yt_sprints WHERE id=?",(id,)).fetchone())
 def update_sprint(self,agile_id,sprint_id,*,name=None,goal=None,archived=None):self._agile_row(agile_id);r=self._sprint_row(agile_id,sprint_id);self.s.execute("UPDATE yt_sprints SET name=?,goal=?,archived=? WHERE id=?",(name or r["name"],goal if goal is not None else r["goal"],archived if archived is not None else r["archived"],sprint_id));return self._sprint(self.s.execute("SELECT * FROM yt_sprints WHERE id=?",(sprint_id,)).fetchone())
 def list_work_items(self,issue_id):i=self._issue_row(issue_id);return [dict(x) for x in self.s.execute("SELECT * FROM yt_work_items WHERE issue_id=? ORDER BY id",(i["id"],)).fetchall()]
 def create_work_item(self,issue_id,duration_minutes,*,text=None):i=self._issue_row(issue_id);r=self.s.execute("INSERT INTO yt_work_items(issue_id,author_id,duration_minutes,text,created_at) VALUES(?,?,?,?,?) RETURNING id",(i["id"],self._actor()["id"],duration_minutes,text,self._time())).fetchone();return dict(self.s.execute("SELECT * FROM yt_work_items WHERE id=?",(r["id"],)).fetchone())
 def list_vcs_changes(self,issue_id):i=self._issue_row(issue_id);return [dict(x) for x in self.s.execute("SELECT * FROM yt_vcs_changes WHERE issue_id=? ORDER BY id",(i["id"],)).fetchall()]
 def _actor(self):return self._user_row(self.actor)
 def _user_row(self,v):
  r=self.s.execute("SELECT * FROM yt_users WHERE id=? OR login=?",(v,v)).fetchone()
  if r is None:raise YTNotFound("user not found")
  return r
 def _project_row(self,v):
  r=self.s.execute("SELECT * FROM yt_projects WHERE id=? OR short_name=?",(v,v)).fetchone()
  if r is None:raise YTNotFound("project not found")
  return r
 def _issue_row(self,v):
  r=self.s.execute("SELECT * FROM yt_issues WHERE id=? OR id_readable=?",(v,v)).fetchone()
  if r is None:raise YTNotFound("issue not found")
  return r
 def _agile_row(self,v):
  r=self.s.execute("SELECT * FROM yt_agiles WHERE id=?",(v,)).fetchone()
  if r is None:raise YTNotFound("agile board not found")
  return r
 def _sprint_row(self,agile_id,sprint_id):
  r=self.s.execute("SELECT * FROM yt_sprints WHERE id=? AND agile_id=?",(sprint_id,agile_id)).fetchone()
  if r is None:raise YTNotFound("sprint not found")
  return r
 def _user(self,x):return {"id":x["id"],"login":x["login"],"fullName":x["full_name"],"email":x["email"],"ringId":x["id"]}
 def _project(self,x):return {"id":x["id"],"shortName":x["short_name"],"name":x["name"]}
 def _issue(self,x):return {"id":x["id"],"idReadable":x["id_readable"],"summary":x["summary"],"description":x["description"],"state":x["state"],"priority":x["priority"],"reporter":self._user(self._user_row(x["reporter_id"])),"assignee":self._user(self._user_row(x["assignee_id"])) if x["assignee_id"] else None,"created":str(x["created_at"]),"updated":str(x["updated_at"])}
 def _comment(self,x):return {"id":str(x["id"]),"text":x["text"],"author":self._user(self._user_row(x["author_id"])),"created":str(x["created_at"])}
 def _sprint(self,x):return {"id":x["id"],"name":x["name"],"goal":x["goal"],"archived":bool(x["archived"]),"start":str(x["start_at"]) if x["start_at"] else None,"finish":str(x["finish_at"]) if x["finish_at"] else None}
 def _time(self):return self.now().isoformat()
