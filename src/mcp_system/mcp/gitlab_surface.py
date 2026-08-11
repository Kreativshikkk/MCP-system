"""OpenAPI-pinned MCP projection of the core GitLab REST v4 API."""

from __future__ import annotations

from typing import Any, Mapping

from .dispatcher import SurfaceSpec, ToolSpec

S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}
SS = {"type": "array", "items": S}
II = {"type": "array", "items": I}
PROJECT = {"project": S}
ISSUE = {**PROJECT, "issue_iid": I}
MR = {**PROJECT, "merge_request_iid": I}
ACTIONS = {"type": "array", "items": {"type": "object", "properties": {"action": {"type": "string", "enum": ["create", "update", "delete", "move"]}, "file_path": S, "previous_path": S, "content": S, "encoding": {"type": "string", "enum": ["text", "base64"]}}, "required": ["action", "file_path"], "additionalProperties": False}}


def gitlab_rest_v4_surface() -> SurfaceSpec:
    specs = (
        ("get_current_user", {}, (), True),
        ("list_users", {"username": S, "search": S}, (), True),
        ("get_group", {"group": S}, ("group",), True),
        ("list_group_members", {"group": S}, ("group",), True),
        ("list_projects", {"group": S}, ("group",), True),
        ("get_project", PROJECT, ("project",), True),
        ("list_labels", PROJECT, ("project",), True),
        ("create_label", {**PROJECT, "name": S, "color": S, "description": S}, ("project", "name", "color"), False),
        ("update_label", {**PROJECT, "name": S, "new_name": S, "color": S, "description": S}, ("project", "name"), False),
        ("delete_label", {**PROJECT, "name": S}, ("project", "name"), False),
        ("list_issues", {**PROJECT, "state": {"type": "string", "enum": ["opened", "closed", "all"]}}, ("project",), True),
        ("get_issue", ISSUE, ("project", "issue_iid"), True),
        ("create_issue", {**PROJECT, "title": S, "description": S, "labels": S, "assignee_ids": II}, ("project", "title"), False),
        ("update_issue", {**ISSUE, "title": S, "description": S, "state_event": {"type": "string", "enum": ["close", "reopen"]}, "labels": S, "add_labels": S, "remove_labels": S, "assignee_ids": II}, ("project", "issue_iid"), False),
        ("delete_issue", ISSUE, ("project", "issue_iid"), False),
        ("list_issue_notes", ISSUE, ("project", "issue_iid"), True),
        ("get_issue_note", {**ISSUE, "note_id": I}, ("project", "issue_iid", "note_id"), True),
        ("create_issue_note", {**ISSUE, "body": S}, ("project", "issue_iid", "body"), False),
        ("update_issue_note", {**ISSUE, "note_id": I, "body": S}, ("project", "issue_iid", "note_id", "body"), False),
        ("delete_issue_note", {**ISSUE, "note_id": I}, ("project", "issue_iid", "note_id"), False),
        ("get_repository_tree", {**PROJECT, "ref": S, "path": S, "recursive": B}, ("project",), True),
        ("compare_repository", {**PROJECT, "from_ref": S, "to_ref": S, "straight": B}, ("project", "from_ref", "to_ref"), True),
        ("get_repository_file", {**PROJECT, "file_path": S, "ref": S}, ("project", "file_path", "ref"), True),
        ("create_repository_file", {**PROJECT, "file_path": S, "branch": S, "content": S, "commit_message": S, "encoding": S}, ("project", "file_path", "branch", "content", "commit_message"), False),
        ("update_repository_file", {**PROJECT, "file_path": S, "branch": S, "content": S, "commit_message": S, "encoding": S}, ("project", "file_path", "branch", "content", "commit_message"), False),
        ("delete_repository_file", {**PROJECT, "file_path": S, "branch": S, "commit_message": S}, ("project", "file_path", "branch", "commit_message"), False),
        ("list_commits", {**PROJECT, "ref_name": S}, ("project",), True),
        ("get_commit", {**PROJECT, "sha": S}, ("project", "sha"), True),
        ("get_commit_diff", {**PROJECT, "sha": S}, ("project", "sha"), True),
        ("create_repository_commit", {**PROJECT, "branch": S, "commit_message": S, "actions": ACTIONS, "start_branch": S, "start_sha": S}, ("project", "branch", "commit_message", "actions"), False),
        ("list_branches", PROJECT, ("project",), True),
        ("get_branch", {**PROJECT, "branch": S}, ("project", "branch"), True),
        ("create_branch", {**PROJECT, "branch": S, "ref": S}, ("project", "branch", "ref"), False),
        ("delete_branch", {**PROJECT, "branch": S}, ("project", "branch"), False),
        ("list_tags", PROJECT, ("project",), True),
        ("get_tag", {**PROJECT, "tag_name": S}, ("project", "tag_name"), True),
        ("create_tag", {**PROJECT, "tag_name": S, "ref": S, "message": S}, ("project", "tag_name", "ref"), False),
        ("delete_tag", {**PROJECT, "tag_name": S}, ("project", "tag_name"), False),
        ("list_merge_requests", {**PROJECT, "state": {"type": "string", "enum": ["opened", "closed", "merged", "all"]}}, ("project",), True),
        ("get_merge_request", MR, ("project", "merge_request_iid"), True),
        ("create_merge_request", {**PROJECT, "title": S, "source_branch": S, "target_branch": S, "description": S, "reviewer_ids": II}, ("project", "title", "source_branch", "target_branch"), False),
        ("update_merge_request", {**MR, "title": S, "description": S, "state_event": S}, ("project", "merge_request_iid"), False),
        ("get_merge_request_changes", MR, ("project", "merge_request_iid"), True),
        ("get_merge_request_approvals", MR, ("project", "merge_request_iid"), True),
        ("approve_merge_request", MR, ("project", "merge_request_iid"), False),
        ("unapprove_merge_request", MR, ("project", "merge_request_iid"), False),
        ("merge_merge_request", {**MR, "sha": S, "merge_commit_message": S}, ("project", "merge_request_iid"), False),
        ("list_merge_request_notes", MR, ("project", "merge_request_iid"), True),
        ("create_merge_request_note", {**MR, "body": S}, ("project", "merge_request_iid", "body"), False),
        ("get_merge_request_note", {**MR, "note_id": I}, ("project", "merge_request_iid", "note_id"), True),
        ("update_merge_request_note", {**MR, "note_id": I, "body": S}, ("project", "merge_request_iid", "note_id", "body"), False),
        ("delete_merge_request_note", {**MR, "note_id": I}, ("project", "merge_request_iid", "note_id"), False),
        ("list_merge_request_discussions", MR, ("project", "merge_request_iid"), True),
        ("create_merge_request_discussion", {**MR, "body": S}, ("project", "merge_request_iid", "body"), False),
        ("resolve_merge_request_discussion", {**MR, "discussion_id": S, "resolved": B}, ("project", "merge_request_iid", "discussion_id", "resolved"), False),
        ("create_merge_request_discussion_note", {**MR, "discussion_id": S, "body": S}, ("project", "merge_request_iid", "discussion_id", "body"), False),
        ("list_merge_request_pipelines", MR, ("project", "merge_request_iid"), True),
        ("create_merge_request_pipeline", MR, ("project", "merge_request_iid"), False),
        ("list_pipelines", PROJECT, ("project",), True),
        ("get_pipeline", {**PROJECT, "pipeline_id": I}, ("project", "pipeline_id"), True),
        ("get_latest_pipeline", {**PROJECT, "ref": S}, ("project",), True),
        ("create_pipeline", {**PROJECT, "ref": S}, ("project", "ref"), False),
        ("retry_pipeline", {**PROJECT, "pipeline_id": I}, ("project", "pipeline_id"), False),
        ("cancel_pipeline", {**PROJECT, "pipeline_id": I}, ("project", "pipeline_id"), False),
        ("list_pipeline_jobs", {**PROJECT, "pipeline_id": I}, ("project", "pipeline_id"), True),
        ("list_jobs", PROJECT, ("project",), True),
        ("get_job", {**PROJECT, "job_id": I}, ("project", "job_id"), True),
        ("get_job_trace", {**PROJECT, "job_id": I}, ("project", "job_id"), True),
        ("retry_job", {**PROJECT, "job_id": I}, ("project", "job_id"), False),
        ("cancel_job", {**PROJECT, "job_id": I}, ("project", "job_id"), False),
        ("play_job", {**PROJECT, "job_id": I}, ("project", "job_id"), False),
        ("list_commit_statuses", {**PROJECT, "sha": S}, ("project", "sha"), True),
        ("set_commit_status", {**PROJECT, "sha": S, "state": S, "name": S, "target_url": S, "description": S}, ("project", "sha", "state"), False),
        ("list_releases", PROJECT, ("project",), True),
        ("get_release", {**PROJECT, "tag_name": S}, ("project", "tag_name"), True),
        ("create_release", {**PROJECT, "tag_name": S, "name": S, "description": S, "released_at": S, "ref": S}, ("project", "tag_name"), False),
        ("update_release", {**PROJECT, "tag_name": S, "name": S, "description": S, "released_at": S}, ("project", "tag_name"), False),
        ("delete_release", {**PROJECT, "tag_name": S}, ("project", "tag_name"), False),
    )
    return SurfaceSpec("gitlab_rest_v4", "gitlab", tuple(_tool(*spec) for spec in specs))


def _tool(operation: str, properties: Mapping[str, Any], required: tuple[str, ...], read_only: bool) -> ToolSpec:
    name = f"gitlab_{operation}"
    schema: dict[str, Any] = {"type": "object", "properties": dict(properties), "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    return ToolSpec(name=name, title=name.removeprefix("gitlab_").replace("_", " ").title(), description=f"GitLab REST v4: {operation.replace('_', ' ')}.", input_schema=schema, operation=operation, argument_renames={}, read_only=read_only, idempotent=read_only)
