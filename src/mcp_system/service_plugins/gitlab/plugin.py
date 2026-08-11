"""GitLab service plugin manifest and bootstrap."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ...errors import ConfigurationError
from ...plugins import Migration, PluginManifest, RelationalSession
from .schema import gitlab_migrations

DEFAULT_LABELS = (("bug", "#d9534f"), ("feature", "#428bca"), ("documentation", "#5bc0de"))


@dataclass(frozen=True, slots=True)
class GitLabPlugin:
    manifest: PluginManifest = PluginManifest(
        plugin_id="gitlab", version="0.1.0", display_name="GitLab REST API replica",
        capabilities=("groups", "users", "projects", "git_data_plane", "repository_files", "commits", "branches", "tags", "issues", "labels", "notes", "merge_requests", "discussions", "approvals", "pipelines", "jobs", "commit_statuses", "releases"),
        contract_source="https://gitlab.com/gitlab-org/gitlab/-/blob/eb75d05715acad3d0ca93f7fbc699e7736470297/doc/api/openapi/openapi_v3.yaml",
        api_version="v4 / GitLab 19.3.0-pre",
        contract_revision="eb75d05715acad3d0ca93f7fbc699e7736470297",
    )

    def migrations(self, storage_kind: str) -> Sequence[Migration]:
        return gitlab_migrations(storage_kind)

    def validate_bootstrap(self, config: Mapping[str, Any]) -> None:
        if not isinstance(config.get("group"), dict) or not config["group"].get("path"):
            raise ConfigurationError("gitlab bootstrap requires group.path")
        users = config.get("users")
        if not isinstance(users, list) or not users:
            raise ConfigurationError("gitlab bootstrap requires users")
        if not any(user.get("access_level", 0) >= 50 for user in users):
            raise ConfigurationError("gitlab bootstrap requires an Owner")
        if not isinstance(config.get("projects", []), list):
            raise ConfigurationError("gitlab bootstrap projects must be an array")

    def seed(self, session: RelationalSession, config: Mapping[str, Any]) -> None:
        self.validate_bootstrap(config)
        created = config.get("created_at", "2026-01-01T00:00:00+00:00")
        group = config["group"]
        session.execute("INSERT INTO gitlab_namespaces(id,name,path,kind,visibility,created_at) VALUES(1,?,?,?,?,?)", (group.get("name", group["path"]), group["path"], "group", group.get("visibility", "private"), created))
        users = config["users"]
        for user_id, user in enumerate(users, 1):
            session.execute("INSERT INTO gitlab_users(id,username,name,email,state,is_admin,created_at) VALUES(?,?,?,?,?,?,?)", (user_id, user["username"], user.get("name", user["username"]), user.get("email"), "active", user.get("admin", False), created))
            session.execute("INSERT INTO gitlab_group_members(namespace_id,user_id,access_level) VALUES(1,?,?)", (user_id, user.get("access_level", 30)))
        label_id = 0
        for project_id, project in enumerate(config.get("projects", []), 1):
            path = project["path"]
            session.execute("""INSERT INTO gitlab_projects(id,namespace_id,name,path,path_with_namespace,description,visibility,archived,default_branch,next_issue_iid,next_merge_request_iid,next_pipeline_iid,created_at,updated_at)
                VALUES(?,1,?,?,?,?,?,?,?,1,1,1,?,?)""", (project_id, project.get("name", path), path, f"{group['path']}/{path}", project.get("description"), project.get("visibility", "private"), False, project.get("default_branch", "main"), created, created))
            for user_id, user in enumerate(users, 1):
                session.execute("INSERT INTO gitlab_project_members(project_id,user_id,access_level) VALUES(?,?,?)", (project_id, user_id, user.get("access_level", 30)))
            for name, color in DEFAULT_LABELS:
                label_id += 1
                session.execute("INSERT INTO gitlab_labels(id,project_id,name,color,description) VALUES(?,?,?,?,NULL)", (label_id, project_id, name, color))

    def create_operations(self, session: RelationalSession, *, actor: str, now: Any | None = None, git_data_plane: Any | None = None) -> Any:
        from .operations import GitLabOperations
        return GitLabOperations(session, actor_username=actor, now=now, git_data_plane=git_data_plane)
