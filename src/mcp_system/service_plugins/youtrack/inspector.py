from dataclasses import dataclass
@dataclass(frozen=True,slots=True)
class YouTrackInspectorAdapter:
 plugin_id:str="youtrack";plugin_version:str="0.1.0"
 def project(self,s,git):
  repos=[]
  for p in s.execute("SELECT * FROM yt_projects ORDER BY short_name").fetchall():
   tickets=[]
   for i in s.execute("SELECT i.*,u.login reporter FROM yt_issues i JOIN yt_users u ON u.id=i.reporter_id WHERE i.project_id=? ORDER BY i.created_at DESC",(p["id"],)).fetchall():
    a=s.execute("SELECT login FROM yt_users WHERE id=?",(i["assignee_id"],)).fetchone() if i["assignee_id"] else None;c=s.execute("SELECT c.*,u.login author FROM yt_comments c JOIN yt_users u ON u.id=c.author_id WHERE c.issue_id=?",(i["id"],)).fetchall();l=s.execute("SELECT l.*,r.id_readable related FROM yt_links l JOIN yt_issues r ON r.id=l.related_issue_id WHERE l.issue_id=?",(i["id"],)).fetchall();tags=s.execute("SELECT t.name FROM yt_issue_tags it JOIN yt_tags t ON t.id=it.tag_id WHERE it.issue_id=? ORDER BY t.name",(i["id"],)).fetchall();tickets.append({"id":i["id"],"number":int(i["id_readable"].split("-")[-1]),"title":f"{i['id_readable']} · {i['summary']}","description":i["description"],"state":"closed" if i["state"] in {"Fixed","Closed"} else "open","stateReason":i["state"],"author":i["reporter"],"labels":[x["name"] for x in tags],"assignees":[a["login"]] if a else [],"comments":[{"id":str(x["id"]),"author":x["author"],"body":x["text"],"createdAt":str(x["created_at"])} for x in c],"iterations":[],"links":[{"id":str(x["id"]),"type":x["link_type"],"direction":"outward","issueKey":x["related"]} for x in l],"createdAt":str(i["created_at"]),"updatedAt":str(i["updated_at"])})
   repos.append({"id":p["id"],"provider":"youtrack","name":p["name"],"fullName":p["short_name"],"visibility":"private","archived":False,"defaultBranch":None,"tickets":tickets,"changeSets":[],"builds":[]})
  return {"provider":{"id":"youtrack","name":"YouTrack"},"repositories":repos}
