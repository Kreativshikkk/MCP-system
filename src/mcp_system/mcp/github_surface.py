"""Declarative GitHub MCP surface backed by provider domain operations."""

from __future__ import annotations

from typing import Any, Mapping

from .dispatcher import SurfaceSpec, ToolSpec


STRING = {"type": "string", "minLength": 1}
NULLABLE_STRING = {"type": ["string", "null"]}
INTEGER = {"type": "integer", "minimum": 1}
BOOLEAN = {"type": "boolean"}
STRINGS = {"type": "array", "items": STRING}
OWNER_REPO = {"owner": STRING, "repo": STRING}
ISSUE = {**OWNER_REPO, "issue_number": INTEGER}
PULL = {**OWNER_REPO, "pull_number": INTEGER}


def github_rest_v3_surface() -> SurfaceSpec:
    tools = (
        _tool("github_get_authenticated_user", "get_authenticated_user", "Get the bound GitHub actor.", {}, (), True),
        _tool("github_get_user", "get_user", "Get a GitHub user by login.", {"username": STRING}, ("username",), True),
        _tool("github_get_organization", "get_organization", "Get an organization.", {"organization": STRING}, ("organization",), True),
        _tool("github_list_organization_members", "list_organization_members", "List organization members.", {"organization": STRING}, ("organization",), True),
        _tool("github_list_repositories", "list_repositories", "List repositories visible to the actor.", {"organization": STRING}, ("organization",), True),
        _tool("github_get_repository", "get_repository", "Get a repository.", OWNER_REPO, ("owner", "repo"), True),
        _tool(
            "github_create_repository", "create_repository", "Create an organization repository.",
            {"organization": STRING, "name": STRING, "description": NULLABLE_STRING, "private": BOOLEAN},
            ("organization", "name"), False,
        ),
        _tool(
            "github_update_repository", "update_repository", "Update repository metadata.",
            {**OWNER_REPO, "name": STRING, "description": NULLABLE_STRING, "private": BOOLEAN, "archived": BOOLEAN, "default_branch": STRING},
            ("owner", "repo"), False,
        ),
        _tool(
            "github_create_commit", "create_commit", "Create real Git blobs, a tree, and a commit from file changes.",
            {**OWNER_REPO, "message": STRING, "author": STRING, "parent_shas": STRINGS, "files": {"type": "object", "additionalProperties": {"type": ["string", "null"]}}},
            ("owner", "repo", "message", "author"), False,
        ),
        _tool("github_list_commits", "list_commits", "List repository commits.", OWNER_REPO, ("owner", "repo"), True),
        _tool(
            "github_create_branch", "create_branch", "Create a branch ref at an existing commit.",
            {**OWNER_REPO, "name": STRING, "head_sha": STRING, "protected": BOOLEAN},
            ("owner", "repo", "name", "head_sha"), False,
        ),
        _tool("github_list_branches", "list_branches", "List repository branches.", OWNER_REPO, ("owner", "repo"), True),
        _tool("github_create_ref", "create_ref", "Create a GitHub Git reference.", {**OWNER_REPO, "ref": STRING, "sha": STRING}, ("owner", "repo", "ref", "sha"), False),
        _tool("github_get_ref", "get_ref", "Get a GitHub Git reference.", {**OWNER_REPO, "ref": STRING}, ("owner", "repo", "ref"), True),
        _tool("github_list_matching_refs", "list_matching_refs", "List GitHub Git references matching a prefix.", {**OWNER_REPO, "ref": STRING}, ("owner", "repo", "ref"), True),
        _tool("github_update_ref", "update_ref", "Update a GitHub Git reference with fast-forward protection.", {**OWNER_REPO, "ref": STRING, "sha": STRING, "force": BOOLEAN}, ("owner", "repo", "ref", "sha"), False),
        _tool("github_list_labels", "list_labels", "List repository issue labels.", OWNER_REPO, ("owner", "repo"), True),
        _tool(
            "github_create_label", "create_label", "Create a repository issue label.",
            {**OWNER_REPO, "name": STRING, "color": STRING, "description": NULLABLE_STRING},
            ("owner", "repo", "name"), False,
        ),
        _tool(
            "github_list_issues", "list_issues", "List repository issues and pull requests.",
            {**OWNER_REPO, "state": {"type": "string", "enum": ["open", "closed", "all"]}},
            ("owner", "repo"), True,
        ),
        _tool("github_get_issue", "get_issue", "Get an issue or pull request as an issue.", ISSUE, ("owner", "repo", "issue_number"), True),
        _tool(
            "github_create_issue", "create_issue", "Create an issue.",
            {**OWNER_REPO, "title": STRING, "body": NULLABLE_STRING, "labels": STRINGS, "assignees": STRINGS},
            ("owner", "repo", "title"), False,
        ),
        _tool(
            "github_update_issue", "update_issue", "Update an issue.",
            {**ISSUE, "title": STRING, "body": NULLABLE_STRING, "state": {"type": "string", "enum": ["open", "closed"]}, "state_reason": {"type": ["string", "null"], "enum": ["completed", "not_planned", "reopened", None]}, "labels": STRINGS, "assignees": STRINGS},
            ("owner", "repo", "issue_number"), False,
        ),
        _tool("github_list_issue_comments", "list_comments", "List issue comments.", ISSUE, ("owner", "repo", "issue_number"), True),
        _tool(
            "github_create_issue_comment", "create_comment", "Create an issue comment.",
            {**ISSUE, "body": STRING}, ("owner", "repo", "issue_number", "body"), False,
        ),
        _tool("github_list_issue_labels", "list_issue_labels", "List labels on an issue.", ISSUE, ("owner", "repo", "issue_number"), True),
        _tool(
            "github_add_issue_labels", "add_issue_labels", "Add labels to an issue.",
            {**ISSUE, "labels": STRINGS}, ("owner", "repo", "issue_number", "labels"), False, idempotent=True,
        ),
        _tool(
            "github_set_issue_labels", "set_issue_labels", "Replace all labels on an issue.",
            {**ISSUE, "labels": STRINGS}, ("owner", "repo", "issue_number", "labels"), False, idempotent=True,
        ),
        _tool(
            "github_remove_issue_label", "remove_issue_label", "Remove one label from an issue.",
            {**ISSUE, "name": STRING}, ("owner", "repo", "issue_number", "name"), False, idempotent=True, destructive=True,
        ),
        _tool("github_remove_all_issue_labels", "remove_all_issue_labels", "Remove every label from an issue.", ISSUE, ("owner", "repo", "issue_number"), False, idempotent=True, destructive=True),
        _tool(
            "github_add_assignees", "add_assignees", "Add issue assignees.",
            {**ISSUE, "assignees": STRINGS}, ("owner", "repo", "issue_number", "assignees"), False, idempotent=True,
        ),
        _tool(
            "github_remove_assignees", "remove_assignees", "Remove issue assignees.",
            {**ISSUE, "assignees": STRINGS}, ("owner", "repo", "issue_number", "assignees"), False, idempotent=True, destructive=True,
        ),
        _tool(
            "github_list_pull_requests", "list_pull_requests", "List pull requests.",
            {**OWNER_REPO, "state": {"type": "string", "enum": ["open", "closed", "all"]}},
            ("owner", "repo"), True,
        ),
        _tool("github_get_pull_request", "get_pull_request", "Get a pull request.", PULL, ("owner", "repo", "pull_number"), True),
        _tool(
            "github_create_pull_request", "create_pull_request", "Create a pull request between branches.",
            {**OWNER_REPO, "title": STRING, "head": STRING, "base": STRING, "body": NULLABLE_STRING, "draft": BOOLEAN},
            ("owner", "repo", "title", "head", "base"), False,
        ),
        _tool(
            "github_update_pull_request", "update_pull_request", "Update a pull request.",
            {**PULL, "title": STRING, "body": NULLABLE_STRING, "state": {"type": "string", "enum": ["open", "closed"]}, "base": STRING},
            ("owner", "repo", "pull_number"), False,
        ),
        _tool("github_list_requested_reviewers", "list_requested_reviewers", "List requested pull-request reviewers.", PULL, ("owner", "repo", "pull_number"), True),
        _tool(
            "github_request_reviewers", "request_reviewers", "Request pull-request reviewers.",
            {**PULL, "reviewers": STRINGS}, ("owner", "repo", "pull_number", "reviewers"), False, idempotent=True,
        ),
        _tool(
            "github_remove_requested_reviewers", "remove_requested_reviewers", "Remove requested reviewers.",
            {**PULL, "reviewers": STRINGS}, ("owner", "repo", "pull_number", "reviewers"), False, idempotent=True, destructive=True,
        ),
        _tool("github_list_reviews", "list_reviews", "List pull-request reviews.", PULL, ("owner", "repo", "pull_number"), True),
        _tool(
            "github_create_review", "create_review", "Submit or create a pending pull-request review.",
            {**PULL, "event": {"type": "string", "enum": ["APPROVE", "REQUEST_CHANGES", "COMMENT", "PENDING"]}, "body": NULLABLE_STRING, "commit_sha": STRING},
            ("owner", "repo", "pull_number", "event"), False,
        ),
        _tool("github_list_review_comments", "list_review_comments", "List inline pull-request review comments.", PULL, ("owner", "repo", "pull_number"), True),
        _tool(
            "github_create_review_comment", "create_review_comment", "Create an inline pull-request review comment.",
            {**PULL, "body": STRING, "path": STRING, "commit_sha": STRING, "line": INTEGER, "side": {"type": "string", "enum": ["LEFT", "RIGHT"]}, "review_id": INTEGER},
            ("owner", "repo", "pull_number", "body", "path"), False,
        ),
        _tool("github_is_pull_request_merged", "is_merged", "Check whether a pull request is merged.", PULL, ("owner", "repo", "pull_number"), True),
        _tool(
            "github_merge_pull_request", "merge_pull_request_api", "Merge a pull request using GitHub's public merge contract.",
            {**PULL, "commit_title": STRING, "commit_message": STRING, "sha": STRING, "merge_method": {"type": "string", "enum": ["merge", "squash", "rebase"]}}, ("owner", "repo", "pull_number"), False, destructive=True,
        ),
        _tool("github_list_workflow_runs", "list_workflow_runs", "List GitHub Actions workflow runs for a repository.", OWNER_REPO, ("owner", "repo"), True),
        _tool("github_list_workflow_jobs", "list_workflow_jobs", "List jobs for a GitHub Actions workflow run.", {**OWNER_REPO, "run_id": INTEGER}, ("owner", "repo", "run_id"), True),
        _tool("github_get_workflow_job", "get_workflow_job", "Get a GitHub Actions workflow job.", {**OWNER_REPO, "job_id": INTEGER}, ("owner", "repo", "job_id"), True),
        _tool("github_get_workflow_job_log", "get_workflow_job_log", "Read the captured log for a GitHub Actions workflow job.", {**OWNER_REPO, "job_id": INTEGER}, ("owner", "repo", "job_id"), True),
        _tool(
            "github_dispatch_workflow", "dispatch_workflow", "Create a GitHub Actions workflow dispatch event.",
            {**OWNER_REPO, "workflow_id": STRING, "ref": STRING, "inputs": {"type": "object", "additionalProperties": {"type": "string"}}},
            ("owner", "repo", "workflow_id", "ref"), False,
        ),
        _tool("github_list_releases", "list_releases", "List GitHub repository releases.", OWNER_REPO, ("owner", "repo"), True),
        _tool(
            "github_create_release", "create_release", "Create a GitHub repository release.",
            {**OWNER_REPO, "tag_name": STRING, "target_commitish": STRING, "name": NULLABLE_STRING, "body": NULLABLE_STRING, "draft": BOOLEAN, "prerelease": BOOLEAN},
            ("owner", "repo", "tag_name"), False,
        ),
    )
    return SurfaceSpec("github_rest_v3", "github", tools)


def _tool(
    name: str,
    operation: str,
    description: str,
    properties: Mapping[str, Any],
    required: tuple[str, ...],
    read_only: bool,
    *,
    idempotent: bool = False,
    destructive: bool = False,
) -> ToolSpec:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": dict(properties),
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return ToolSpec(
        name=name,
        title=name.removeprefix("github_").replace("_", " ").title(),
        description=description,
        input_schema=schema,
        operation=operation,
        argument_renames={"repo": "repository"},
        read_only=read_only,
        idempotent=idempotent or read_only,
        destructive=destructive,
    )
