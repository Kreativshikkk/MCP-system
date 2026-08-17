from dataclasses import dataclass
from typing import Any,Mapping
from ...errors import ConfigurationError
from ...plugins import PluginManifest
from .schema import youtrack_migrations
@dataclass(frozen=True,slots=True)
class YouTrackPlugin:
 manifest:PluginManifest=PluginManifest(plugin_id="youtrack",version="0.1.0",display_name="YouTrack REST replica",capabilities=("users","projects","issues","tags","comments","links","agiles","sprints","work_items","vcs_changes"),contract_source="https://www.jetbrains.com/help/youtrack/devportal/api-resources.html",api_version="current REST",contract_revision="sha256:32730aedd0ab94f6fefc4f55d21b8bf44145337e297280618217331b6bf0a639")
 def migrations(self,k):return youtrack_migrations(k)
 def validate_bootstrap(self,c):
  if not c.get("users") or not any(x.get("admin") for x in c["users"]):raise ConfigurationError("youtrack bootstrap requires admin users")
  if not isinstance(c.get("project"),dict) or not c["project"].get("short_name"):raise ConfigurationError("youtrack bootstrap requires project.short_name")
 def seed(self,s,c):
  self.validate_bootstrap(c)
  for n,u in enumerate(c["users"],1):s.execute("INSERT INTO yt_users(id,login,full_name,email,admin) VALUES(?,?,?,?,?)",(f"user-{n}",u["login"],u.get("full_name",u["login"]),u.get("email"),u.get("admin",False)))
  p=c["project"];s.execute("INSERT INTO yt_projects(id,short_name,name,leader_id,next_issue) VALUES('project-1',?,?,?,1)",(p["short_name"],p.get("name",p["short_name"]),p.get("leader_id","user-2")));s.execute("INSERT INTO yt_agiles(id,name,project_id) VALUES('agile-1',?,'project-1')",(p.get("board_name","Product Board"),))
 def create_operations(self,s,*,actor,now=None,git_data_plane=None):
  from .operations import YouTrackOperations
  return YouTrackOperations(s,actor=actor,now=now)
