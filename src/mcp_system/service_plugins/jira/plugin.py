"""Jira Cloud service plugin manifest and bootstrap."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ...errors import ConfigurationError
from ...plugins import Migration, PluginManifest, RelationalSession
from .schema import jira_migrations


@dataclass(frozen=True, slots=True)
class JiraPlugin:
    manifest: PluginManifest = PluginManifest(
        plugin_id="jira", version="0.1.0", display_name="Jira Cloud REST API replica",
        capabilities=("users", "projects", "issues", "comments", "transitions", "issue_links", "boards", "sprints"),
        contract_source="https://developer.atlassian.com/cloud/jira/platform/rest/v3/",
        api_version="Platform v3 / Software Agile v1", contract_revision="1.8516.75",
    )

    def migrations(self, storage_kind: str) -> Sequence[Migration]:
        return jira_migrations(storage_kind)

    def validate_bootstrap(self, config: Mapping[str, Any]) -> None:
        users = config.get("users")
        projects = config.get("projects")
        if not isinstance(users, list) or not users:
            raise ConfigurationError("jira bootstrap requires users")
        if not any(bool(user.get("admin")) for user in users):
            raise ConfigurationError("jira bootstrap requires an admin")
        if not isinstance(projects, list) or not projects:
            raise ConfigurationError("jira bootstrap requires projects")
        accounts = {user.get("account_id") for user in users}
        if None in accounts or len(accounts) != len(users):
            raise ConfigurationError("jira user account_id values must be present and unique")
        keys = {project.get("key") for project in projects}
        if None in keys or len(keys) != len(projects):
            raise ConfigurationError("jira project keys must be present and unique")
        for project in projects:
            if project.get("lead") not in accounts:
                raise ConfigurationError("jira project lead must reference a seeded user")

    def seed(self, session: RelationalSession, config: Mapping[str, Any]) -> None:
        self.validate_bootstrap(config)
        created = config.get("created_at", "2026-01-01T00:00:00+00:00")
        for user_id, user in enumerate(config["users"], 1):
            session.execute("INSERT INTO jira_users(id,account_id,display_name,email,active,is_admin,created_at) VALUES(?,?,?,?,?,?,?)", (user_id, user["account_id"], user.get("display_name", user["account_id"]), user.get("email"), True, bool(user.get("admin")), created))
        for project_id, project in enumerate(config["projects"], 1):
            key = project["key"].upper()
            session.execute("INSERT INTO jira_projects(id,key,name,description,lead_account_id,next_issue_number,created_at) VALUES(?,?,?,?,?,1,?)", (project_id, key, project.get("name", key), project.get("description"), project["lead"], created))
            for user in config["users"]:
                session.execute("INSERT INTO jira_project_members(project_id,account_id,role) VALUES(?,?,?)", (project_id, user["account_id"], user.get("role", "Member")))
            session.execute("INSERT INTO jira_boards(id,name,board_type,project_id,created_at) VALUES(?,?,?,?,?)", (project_id, project.get("board_name", f"{key} board"), project.get("board_type", "scrum"), project_id, created))

    def create_operations(self, session: RelationalSession, *, actor: str, now: Any | None = None, git_data_plane: Any | None = None) -> Any:
        from .operations import JiraOperations
        return JiraOperations(session, actor_account_id=actor, now=now)
