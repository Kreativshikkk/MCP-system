"""OpenAPI-pinned MCP adapter for selected Jira Platform and Software APIs."""

from __future__ import annotations

from typing import Any, Mapping

from .dispatcher import SurfaceSpec, ToolSpec

S = {"type": "string"}
I = {"type": "integer", "minimum": 0}
B = {"type": "boolean"}
SS = {"type": "array", "items": S}
O = {"type": "object"}
OS = {"type": "array", "items": O}
ISSUE_UPDATE = {"fields": O, "update": O, "properties": OS, "transition": O, "historyMetadata": O}


def jira_rest_v3_surface() -> SurfaceSpec:
    specs = (
        ("get_current_user", {}, (), True, {}),
        ("list_users", {"startAt": I, "maxResults": I}, (), True, {"startAt": "start_at", "maxResults": "max_results"}),
        ("list_projects", {"startAt": I, "maxResults": I, "query": S}, (), True, {"startAt": "start_at", "maxResults": "max_results"}),
        ("get_project", {"projectIdOrKey": S}, ("projectIdOrKey",), True, {"projectIdOrKey": "project_id_or_key"}),
        ("list_priorities", {}, (), True, {}),
        ("list_issue_types", {}, (), True, {}),
        ("search_issues", {"jql": S, "maxResults": I, "nextPageToken": S, "fields": SS, "reconcileIssues": {"type": "array", "items": I}}, ("jql",), True, {"maxResults": "max_results", "nextPageToken": "next_page_token", "reconcileIssues": "reconcile_issues"}),
        ("get_issue", {"issueIdOrKey": S}, ("issueIdOrKey",), True, {"issueIdOrKey": "issue_id_or_key"}),
        ("create_issue", ISSUE_UPDATE, ("fields",), False, {"historyMetadata": "history_metadata"}),
        ("update_issue", {"issueIdOrKey": S, **ISSUE_UPDATE}, ("issueIdOrKey",), False, {"issueIdOrKey": "issue_id_or_key", "historyMetadata": "history_metadata"}),
        ("list_transitions", {"issueIdOrKey": S}, ("issueIdOrKey",), True, {"issueIdOrKey": "issue_id_or_key"}),
        ("transition_issue", {"issueIdOrKey": S, **ISSUE_UPDATE}, ("issueIdOrKey", "transition"), False, {"issueIdOrKey": "issue_id_or_key", "historyMetadata": "history_metadata"}),
        ("list_comments", {"issueIdOrKey": S, "startAt": I, "maxResults": I}, ("issueIdOrKey",), True, {"issueIdOrKey": "issue_id_or_key", "startAt": "start_at", "maxResults": "max_results"}),
        ("add_comment", {"issueIdOrKey": S, "body": O, "visibility": O, "properties": OS}, ("issueIdOrKey", "body"), False, {"issueIdOrKey": "issue_id_or_key"}),
        ("create_issue_link", {"type": O, "outwardIssue": O, "inwardIssue": O, "comment": O}, ("type", "outwardIssue", "inwardIssue"), False, {"outwardIssue": "outward_issue", "inwardIssue": "inward_issue"}),
        ("list_boards", {"startAt": I, "maxResults": I, "projectKeyOrId": S, "type": {"type": "string", "enum": ["scrum", "kanban"]}, "name": S}, (), True, {"startAt": "start_at", "maxResults": "max_results", "projectKeyOrId": "project_key_or_id", "type": "board_type"}),
        ("list_sprints", {"boardId": I, "startAt": I, "maxResults": I, "state": S}, ("boardId",), True, {"boardId": "board_id", "startAt": "start_at", "maxResults": "max_results"}),
        ("create_sprint", {"name": S, "goal": S, "startDate": S, "endDate": S, "originBoardId": I}, ("name", "originBoardId"), False, {"startDate": "start_date", "endDate": "end_date", "originBoardId": "board_id"}),
        ("get_sprint", {"sprintId": I}, ("sprintId",), True, {"sprintId": "sprint_id"}),
        ("update_sprint", {"sprintId": I, "name": S, "goal": S, "startDate": S, "endDate": S, "state": {"type": "string", "enum": ["future", "active", "closed"]}}, ("sprintId",), False, {"sprintId": "sprint_id", "startDate": "start_date", "endDate": "end_date"}),
        ("list_sprint_issues", {"sprintId": I, "startAt": I, "maxResults": I, "jql": S, "fields": SS}, ("sprintId",), True, {"sprintId": "sprint_id", "startAt": "start_at", "maxResults": "max_results"}),
        ("move_issues_to_sprint", {"sprintId": I, "issues": SS, "rankAfterIssue": S, "rankBeforeIssue": S, "rankCustomFieldId": I}, ("sprintId", "issues"), False, {"sprintId": "sprint_id", "rankAfterIssue": "rank_after_issue", "rankBeforeIssue": "rank_before_issue", "rankCustomFieldId": "rank_custom_field_id"}),
    )
    return SurfaceSpec("jira_rest_v3", "jira", tuple(_tool(*spec) for spec in specs))


def _tool(operation: str, properties: Mapping[str, Any], required: tuple[str, ...], read_only: bool, renames: Mapping[str, str]) -> ToolSpec:
    schema: dict[str, Any] = {"type": "object", "properties": dict(properties), "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    name = f"jira_{operation}"
    api = "Jira Software Agile API" if operation in {"list_boards", "list_sprints", "create_sprint", "get_sprint", "update_sprint", "list_sprint_issues", "move_issues_to_sprint"} else "Jira Cloud REST API v3"
    return ToolSpec(name=name, title=name.removeprefix("jira_").replace("_", " ").title(), description=f"{api}: {operation.replace('_', ' ')}.", input_schema=schema, operation=operation, argument_renames=renames, read_only=read_only, idempotent=read_only or operation == "create_issue_link")
