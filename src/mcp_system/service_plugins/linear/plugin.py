from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from ...errors import ConfigurationError
from ...plugins import Migration, PluginManifest, RelationalSession
from .schema import linear_migrations


@dataclass(frozen=True, slots=True)
class LinearPlugin:
    manifest: PluginManifest = PluginManifest(plugin_id="linear",version="0.1.0",display_name="Linear GraphQL replica",capabilities=("users","teams","workflow_states","labels","projects","cycles","issues","comments","issue_relations"),contract_source="https://linear.app/developers/graphql",api_version="GraphQL",contract_revision="sha256:057736cf75b83075fd2dfb5276c93e9777fec8c881eb4b0082b4f480d3c8561a")
    def migrations(self, storage_kind: str) -> Sequence[Migration]: return linear_migrations(storage_kind)
    def validate_bootstrap(self, config: Mapping[str, Any]) -> None:
        if not isinstance(config.get("users"),list) or not config["users"]: raise ConfigurationError("linear bootstrap requires users")
        if not isinstance(config.get("team"),dict) or not config["team"].get("key"): raise ConfigurationError("linear bootstrap requires team.key")
        if not any(user.get("admin") for user in config["users"]): raise ConfigurationError("linear bootstrap requires an admin")
    def seed(self, session: RelationalSession, config: Mapping[str, Any]) -> None:
        self.validate_bootstrap(config); created=config.get("created_at","2026-01-01T00:00:00+00:00"); team=config["team"]
        for index,user in enumerate(config["users"],1): session.execute("INSERT INTO linear_users(id,email,name,display_name,active,admin,created_at) VALUES(?,?,?,?,?,?,?)",(f"user-{index}",user["email"],user.get("name",user["email"]),user.get("display_name",user.get("name",user["email"])),True,user.get("admin",False),created))
        session.execute("INSERT INTO linear_teams(id,key,name,description,next_issue_number) VALUES('team-1',?,?,?,1)",(team["key"],team.get("name",team["key"]),team.get("description")))
        for index,user in enumerate(config["users"],1): session.execute("INSERT INTO linear_team_members(team_id,user_id,role) VALUES('team-1',?,?)",(f"user-{index}",user.get("role","member")))
        for index,(name,kind) in enumerate((("Backlog","backlog"),("Todo","unstarted"),("In Progress","started"),("Done","completed"),("Canceled","canceled")),1): session.execute("INSERT INTO linear_workflow_states(id,team_id,name,type,position) VALUES(?,'team-1',?,?,?)",(f"state-{index}",name,kind,index))
        for index,(name,color) in enumerate((("Bug","#ef4444"),("Feature","#3b82f6"),("Improvement","#8b5cf6")),1): session.execute("INSERT INTO linear_labels(id,team_id,name,color) VALUES(?,'team-1',?,?)",(f"label-{index}",name,color))
    def create_operations(self, session: RelationalSession, *, actor: str, now: Any|None=None, git_data_plane: Any|None=None) -> Any:
        from .operations import LinearOperations
        return LinearOperations(session,actor=actor,now=now)
