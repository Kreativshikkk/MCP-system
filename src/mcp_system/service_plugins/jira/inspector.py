"""Jira projection into the provider-neutral Inspector model."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping

from ...git_storage import GitServiceDataPlane
from ...plugins import RelationalSession


@dataclass(frozen=True, slots=True)
class JiraInspectorAdapter:
    plugin_id: str = "jira"
    plugin_version: str = "0.1.0"

    def project(self, session: RelationalSession, git_data_plane: GitServiceDataPlane | None) -> Mapping[str, Any]:
        projects = session.execute("SELECT * FROM jira_projects ORDER BY key").fetchall()
        return {"provider": {"id": "jira", "name": "Jira"}, "repositories": [self._project(session, row) for row in projects]}

    def _project(self, session: RelationalSession, project: Mapping[str, Any]) -> dict[str, Any]:
        rows = session.execute("SELECT i.*,u.display_name reporter_name FROM jira_issues i JOIN jira_users u ON u.account_id=i.reporter_account_id WHERE i.project_id=? ORDER BY i.issue_number DESC", (project["id"],)).fetchall()
        tickets = []
        for issue in rows:
            labels = session.execute("SELECT label FROM jira_issue_labels WHERE issue_id=? ORDER BY lower(label)", (issue["id"],)).fetchall()
            comments = session.execute("SELECT c.*,u.display_name author_name FROM jira_comments c JOIN jira_users u ON u.account_id=c.author_account_id WHERE c.issue_id=? ORDER BY c.id", (issue["id"],)).fetchall()
            assignee = session.execute("SELECT display_name FROM jira_users WHERE account_id=?", (issue["assignee_account_id"],)).fetchone() if issue["assignee_account_id"] else None
            sprint_rows = session.execute("SELECT s.* FROM jira_sprint_issues si JOIN jira_sprints s ON s.id=si.sprint_id WHERE si.issue_id=? ORDER BY s.id", (issue["id"],)).fetchall()
            link_rows = session.execute("SELECT l.*,o.issue_key outward_key,i.issue_key inward_key FROM jira_issue_links l JOIN jira_issues o ON o.id=l.outward_issue_id JOIN jira_issues i ON i.id=l.inward_issue_id WHERE l.outward_issue_id=? OR l.inward_issue_id=? ORDER BY l.id", (issue["id"], issue["id"])).fetchall()
            iterations = [{"id": str(row["id"]), "name": row["name"], "state": row["state"], "goal": row["goal"]} for row in sprint_rows]
            links = [{"id": str(row["id"]), "type": row["link_type"], "direction": "outward" if row["outward_issue_id"] == issue["id"] else "inward", "issueKey": row["inward_key"] if row["outward_issue_id"] == issue["id"] else row["outward_key"]} for row in link_rows]
            tickets.append({"id": str(issue["id"]), "number": issue["issue_number"], "title": f"{issue['issue_key']} · {issue['summary']}", "description": _display_content(issue["description"]), "state": "closed" if issue["status"] == "Done" else "open", "stateReason": issue["status"], "author": issue["reporter_name"], "labels": [row["label"] for row in labels], "assignees": [assignee["display_name"]] if assignee else [], "comments": [{"id": str(row["id"]), "author": row["author_name"], "body": _display_content(row["body"]), "createdAt": _json_time(row["created_at"])} for row in comments], "iterations": iterations, "links": links, "createdAt": _json_time(issue["created_at"]), "updatedAt": _json_time(issue["updated_at"])})
        return {"id": str(project["id"]), "provider": "jira", "name": project["name"], "fullName": project["key"], "visibility": "private", "archived": False, "defaultBranch": None, "tickets": tickets, "changeSets": [], "builds": []}


def _json_time(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _display_content(value: Any) -> Any:
    if not isinstance(value, str) or not value.startswith("\x1ejson:"):
        return value
    document = json.loads(value.removeprefix("\x1ejson:"))
    texts: list[str] = []
    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "text" and isinstance(node.get("text"), str): texts.append(node["text"])
            for child in node.get("content", []): visit(child)
        elif isinstance(node, list):
            for child in node: visit(child)
    visit(document)
    return "\n".join(texts)
