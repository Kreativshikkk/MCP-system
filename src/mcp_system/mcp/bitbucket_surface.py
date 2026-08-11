"""Contract-pinned MCP adapter for selected Bitbucket Cloud API 2.0 calls."""

from __future__ import annotations

from typing import Any, Mapping

from .dispatcher import SurfaceSpec, ToolSpec

S = {"type": "string", "minLength": 1}; I = {"type": "integer", "minimum": 1}
SS = {"type": "array", "items": S}; FILES = {"type": "object", "additionalProperties": {"type": ["string", "null"]}}
REPO = {"workspace": S, "repo_slug": S}; ISSUE = {**REPO, "issue_id": I}; PR = {**REPO, "pull_request_id": I}


def bitbucket_cloud_v2_surface() -> SurfaceSpec:
    specs = (
        ("get_current_user", {}, (), True),
        ("get_workspace", {"workspace": S}, ("workspace",), True),
        ("list_workspace_members", {"workspace": S}, ("workspace",), True),
        ("list_repositories", {"workspace": S}, ("workspace",), True),
        ("get_repository", REPO, ("workspace", "repo_slug"), True),
        ("list_commits", {**REPO, "include": S}, ("workspace", "repo_slug"), True),
        ("get_commit", {**REPO, "commit": S}, ("workspace", "repo_slug", "commit"), True),
        ("create_commit", {**REPO, "branch": S, "message": S, "files": FILES, "parents": SS}, ("workspace", "repo_slug", "branch", "message", "files"), False),
        ("get_file", {**REPO, "commit": S, "path": S}, ("workspace", "repo_slug", "commit", "path"), True),
        ("get_diff", {**REPO, "spec": S}, ("workspace", "repo_slug", "spec"), True),
        ("list_branches", REPO, ("workspace", "repo_slug"), True),
        ("create_branch", {**REPO, "name": S, "target": S}, ("workspace", "repo_slug", "name", "target"), False),
        ("get_branch", {**REPO, "name": S}, ("workspace", "repo_slug", "name"), True),
        ("delete_branch", {**REPO, "name": S}, ("workspace", "repo_slug", "name"), False),
        ("list_issues", {**REPO, "state": S}, ("workspace", "repo_slug"), True),
        ("create_issue", {**REPO, "title": S, "content": S, "kind": S, "priority": S, "assignee": S}, ("workspace", "repo_slug", "title"), False),
        ("get_issue", ISSUE, ("workspace", "repo_slug", "issue_id"), True),
        ("update_issue", {**ISSUE, "title": S, "content": S, "state": S, "assignee": S}, ("workspace", "repo_slug", "issue_id"), False),
        ("list_issue_comments", ISSUE, ("workspace", "repo_slug", "issue_id"), True),
        ("create_issue_comment", {**ISSUE, "content": S}, ("workspace", "repo_slug", "issue_id", "content"), False),
        ("list_pull_requests", {**REPO, "state": S}, ("workspace", "repo_slug"), True),
        ("create_pull_request", {**REPO, "title": S, "source_branch": S, "destination_branch": S, "description": S, "reviewers": SS}, ("workspace", "repo_slug", "title", "source_branch", "destination_branch"), False),
        ("get_pull_request", PR, ("workspace", "repo_slug", "pull_request_id"), True),
        ("get_pull_request_diff", PR, ("workspace", "repo_slug", "pull_request_id"), True),
        ("list_pull_request_comments", PR, ("workspace", "repo_slug", "pull_request_id"), True),
        ("create_pull_request_comment", {**PR, "content": S}, ("workspace", "repo_slug", "pull_request_id", "content"), False),
        ("approve_pull_request", PR, ("workspace", "repo_slug", "pull_request_id"), False),
        ("request_changes", PR, ("workspace", "repo_slug", "pull_request_id"), False),
        ("merge_pull_request", {**PR, "message": S}, ("workspace", "repo_slug", "pull_request_id"), False),
        ("list_pipelines", REPO, ("workspace", "repo_slug"), True),
        ("create_pipeline", {**REPO, "ref_name": S}, ("workspace", "repo_slug", "ref_name"), False),
        ("get_pipeline", {**REPO, "pipeline_uuid": S}, ("workspace", "repo_slug", "pipeline_uuid"), True),
        ("list_pipeline_steps", {**REPO, "pipeline_uuid": S}, ("workspace", "repo_slug", "pipeline_uuid"), True),
        ("get_pipeline_step_log", {**REPO, "pipeline_uuid": S, "step_uuid": S}, ("workspace", "repo_slug", "pipeline_uuid", "step_uuid"), True),
        ("list_commit_statuses", {**REPO, "commit": S}, ("workspace", "repo_slug", "commit"), True),
        ("create_commit_status", {**REPO, "commit": S, "key": S, "state": S, "name": S, "url": S, "description": S}, ("workspace", "repo_slug", "commit", "key", "state"), False),
    )
    return SurfaceSpec("bitbucket_cloud_v2", "bitbucket", tuple(_tool(*spec) for spec in specs))


def _tool(operation: str, properties: Mapping[str, Any], required: tuple[str, ...], read_only: bool) -> ToolSpec:
    schema: dict[str, Any] = {"type": "object", "properties": dict(properties), "additionalProperties": False}
    if required: schema["required"] = list(required)
    return ToolSpec(name=f"bitbucket_{operation}", title=operation.replace("_", " ").title(), description=f"Bitbucket Cloud API 2.0: {operation.replace('_', ' ')}.", input_schema=schema, operation=operation, argument_renames={}, read_only=read_only, idempotent=read_only)
