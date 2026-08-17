"""Bitbucket Cloud plugin manifest and deterministic bootstrap."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ...errors import ConfigurationError
from ...plugins import Migration, PluginManifest, RelationalSession
from .schema import bitbucket_migrations


@dataclass(frozen=True, slots=True)
class BitbucketPlugin:
    manifest: PluginManifest = PluginManifest(
        plugin_id="bitbucket", version="0.1.0", display_name="Bitbucket Cloud API replica",
        capabilities=("users", "workspaces", "repositories", "git_data_plane", "commits", "branches", "tags", "issues", "comments", "pull_requests", "reviews", "pipelines", "commit_statuses"),
        contract_source="https://api.bitbucket.org/swagger.json",
        api_version="2.0",
        contract_revision="sha256:dc11be99fe57eb991194de80cdfe75e425cd0f590e6cf83e9dc3d2a22d4943de",
    )

    def migrations(self, storage_kind: str) -> Sequence[Migration]:
        return bitbucket_migrations(storage_kind)

    def validate_bootstrap(self, config: Mapping[str, Any]) -> None:
        workspace = config.get("workspace")
        if not isinstance(workspace, dict) or not workspace.get("slug"):
            raise ConfigurationError("bitbucket bootstrap requires workspace.slug")
        users = config.get("users")
        if not isinstance(users, list) or not users:
            raise ConfigurationError("bitbucket bootstrap requires users")
        if not any(user.get("permission") == "admin" for user in users):
            raise ConfigurationError("bitbucket bootstrap requires an admin user")
        if not isinstance(config.get("repositories", []), list):
            raise ConfigurationError("bitbucket bootstrap repositories must be an array")

    def seed(self, session: RelationalSession, config: Mapping[str, Any]) -> None:
        self.validate_bootstrap(config)
        created = config.get("created_at", "2026-01-01T00:00:00+00:00")
        workspace = config["workspace"]
        session.execute("INSERT INTO bitbucket_workspaces(id,uuid,slug,name,is_private,created_at) VALUES(1,?,?,?,?,?)", ("{workspace-1}", workspace["slug"], workspace.get("name", workspace["slug"]), workspace.get("is_private", True), created))
        for user_id, user in enumerate(config["users"], 1):
            session.execute("INSERT INTO bitbucket_users(id,uuid,username,display_name,email,is_admin,created_at) VALUES(?,?,?,?,?,?,?)", (user_id, f"{{user-{user_id}}}", user["username"], user.get("display_name", user["username"]), user.get("email"), user.get("permission") == "admin", created))
            session.execute("INSERT INTO bitbucket_workspace_members(workspace_id,user_id,permission) VALUES(1,?,?)", (user_id, user.get("permission", "write")))
        for repo_id, repo in enumerate(config.get("repositories", []), 1):
            session.execute("INSERT INTO bitbucket_repositories(id,uuid,workspace_id,slug,name,description,is_private,mainbranch,next_issue_id,next_pull_request_id,next_pipeline_number,created_at,updated_at) VALUES(?,?,1,?,?,?,?,?,1,1,1,?,?)", (repo_id, f"{{repository-{repo_id}}}", repo["slug"], repo.get("name", repo["slug"]), repo.get("description"), repo.get("is_private", True), repo.get("mainbranch", "main"), created, created))
            for user_id, user in enumerate(config["users"], 1):
                session.execute("INSERT INTO bitbucket_repository_members(repository_id,user_id,permission) VALUES(?,?,?)", (repo_id, user_id, user.get("permission", "write")))

    def create_operations(self, session: RelationalSession, *, actor: str, now: Any | None = None, git_data_plane: Any | None = None) -> Any:
        from .operations import BitbucketOperations
        return BitbucketOperations(session, actor=actor, now=now, git_data_plane=git_data_plane)
