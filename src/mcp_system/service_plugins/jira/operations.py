"""Transactional operations for the bounded Jira Cloud v3 domain."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any, Callable, Mapping

from ...errors import ServiceOperationError
from ...plugins import RelationalSession


class JiraOperationError(ServiceOperationError):
    status_code = 500
    error = "internal_error"


class JiraNotFound(JiraOperationError):
    status_code = 404
    error = "not_found"


class JiraForbidden(JiraOperationError):
    status_code = 403
    error = "forbidden"


class JiraValidationError(JiraOperationError):
    status_code = 400
    error = "validation_failed"


class JiraConflict(JiraOperationError):
    status_code = 409
    error = "conflict"


class JiraOperations:
    def __init__(self, session: RelationalSession, *, actor_account_id: str, now: Callable[[], datetime] | None = None) -> None:
        self.session = session
        self.actor_account_id = actor_account_id
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._actor: Mapping[str, Any] | None = None

    def get_current_user(self) -> dict[str, Any]:
        return self._user(self._require_actor())

    def list_users(self, query: str | None = None, start_at: int = 0, max_results: int = 50) -> list[dict[str, Any]]:
        rows = self.session.execute("SELECT * FROM jira_users ORDER BY lower(display_name)").fetchall()
        if query:
            needle = query.casefold()
            rows = [row for row in rows if needle in row["display_name"].casefold() or needle in row["account_id"].casefold()]
        return [self._user(row) for row in rows[start_at:start_at + max_results]]

    def list_projects(self, start_at: int = 0, max_results: int = 50, query: str | None = None) -> dict[str, Any]:
        rows = self.session.execute("SELECT * FROM jira_projects ORDER BY key").fetchall()
        if query:
            needle = query.casefold()
            rows = [row for row in rows if needle in row["key"].casefold() or needle in row["name"].casefold()]
        values = [self._project(row) for row in rows[start_at:start_at + max_results]]
        return {"startAt": start_at, "maxResults": max_results, "total": len(rows), "isLast": start_at + len(values) >= len(rows), "values": values}

    def get_project(self, project_id_or_key: str) -> dict[str, Any]:
        return self._project(self._require_project(project_id_or_key))

    def list_priorities(self) -> list[dict[str, Any]]:
        return [{"id": str(index), "name": name} for index, name in enumerate(("Highest", "High", "Medium", "Low", "Lowest"), 1)]

    def list_issue_types(self) -> list[dict[str, Any]]:
        return [{"id": str(index), "name": name, "subtask": name == "Subtask"} for index, name in enumerate(("Epic", "Story", "Task", "Bug", "Subtask"), 10000)]

    def search_issues(self, jql: str, max_results: int = 50, next_page_token: str | None = None, fields: list[str] | None = None, reconcile_issues: list[int] | None = None) -> dict[str, Any]:
        sql = "SELECT i.* FROM jira_issues i JOIN jira_projects p ON p.id=i.project_id WHERE 1=1"
        params: list[Any] = []
        project = _jql_value(jql, "project")
        status = _jql_value(jql, "status")
        assignee = _jql_value(jql, "assignee")
        if project: sql += " AND lower(p.key)=lower(?)"; params.append(project)
        if status: sql += " AND lower(i.status)=lower(?)"; params.append(status)
        if assignee:
            value = self.actor_account_id if assignee.casefold() == "currentuser()" else assignee
            sql += " AND lower(i.assignee_account_id)=lower(?)"; params.append(value)
        sql += " ORDER BY i.updated_at DESC, i.id DESC"
        rows = self.session.execute(sql, tuple(params)).fetchall()
        start = int(next_page_token or 0)
        page = rows[start:start + max_results]
        result: dict[str, Any] = {"issues": [self._issue(row) for row in page], "isLast": start + len(page) >= len(rows)}
        if not result["isLast"]: result["nextPageToken"] = str(start + len(page))
        return result

    def get_issue(self, issue_id_or_key: str) -> dict[str, Any]:
        return self._issue(self._require_issue(issue_id_or_key))

    def create_issue(self, project: str | None = None, summary: str | None = None, issue_type: str = "Task", description: Any | None = None, priority: str = "Medium", assignee: str | None = None, labels: list[str] | None = None, fields: Mapping[str, Any] | None = None, update: Mapping[str, Any] | None = None, properties: list[Mapping[str, Any]] | None = None, transition: Mapping[str, Any] | None = None, history_metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
        native = fields is not None
        if fields is not None:
            project = _named(fields.get("project"), "key", "id")
            summary = fields.get("summary")
            issue_type = _catalog_name(fields.get("issuetype"), self.list_issue_types(), "issuetype")
            description = fields.get("description")
            priority = _catalog_name(fields.get("priority", {"name": "Medium"}), self.list_priorities(), "priority")
            assignee = _named(fields.get("assignee"), "accountId", "id")
            labels = fields.get("labels", [])
        if project is None or not isinstance(summary, str): raise JiraValidationError("fields.project and fields.summary are required")
        actor = self._require_actor()
        project_row = self._require_project(project)
        self._require_member(project_row["id"], actor["account_id"])
        if not summary.strip():
            raise JiraValidationError("summary must not be empty")
        self._validate_choice(issue_type, {"Epic", "Story", "Task", "Bug", "Subtask"}, "issue_type")
        self._validate_choice(priority, {"Highest", "High", "Medium", "Low", "Lowest"}, "priority")
        if assignee is not None:
            self._require_user(assignee); self._require_member(project_row["id"], assignee)
        number = project_row["next_issue_number"]
        key = f"{project_row['key']}-{number}"
        now = self._time()
        result = self.session.execute("INSERT INTO jira_issues(project_id,issue_number,issue_key,summary,description,issue_type,status,priority,reporter_account_id,assignee_account_id,resolution,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?,?) RETURNING id", (project_row["id"], number, key, summary, _store_content(description), issue_type, "To Do", priority, actor["account_id"], assignee, now, now)).fetchone()
        self.session.execute("UPDATE jira_projects SET next_issue_number=? WHERE id=?", (number + 1, project_row["id"]))
        issue_id = result["id"]
        for label in sorted(set(labels or []), key=str.casefold):
            self.session.execute("INSERT INTO jira_issue_labels(issue_id,label) VALUES(?,?)", (issue_id, label))
        if transition:
            self.transition_issue(str(issue_id), transition=transition)
        return {"id": str(issue_id), "key": key, "self": f"/rest/api/3/issue/{issue_id}"} if native else self._issue(self._require_issue(str(issue_id)))

    def update_issue(self, issue_id_or_key: str, summary: str | None = None, description: Any | None = None, priority: str | None = None, assignee: str | None = None, labels: list[str] | None = None, fields: Mapping[str, Any] | None = None, update: Mapping[str, Any] | None = None, properties: list[Mapping[str, Any]] | None = None, transition: Mapping[str, Any] | None = None, history_metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if fields is not None:
            summary = fields.get("summary")
            description = fields.get("description")
            priority = _catalog_name(fields.get("priority"), self.list_priorities(), "priority") if "priority" in fields else None
            assignee = _named(fields.get("assignee"), "accountId", "id") if "assignee" in fields else None
            labels = fields.get("labels")
        issue = self._require_issue(issue_id_or_key)
        self._require_member(issue["project_id"], self._require_actor()["account_id"])
        updates: list[str] = []; params: list[Any] = []
        if summary is not None:
            if not summary.strip(): raise JiraValidationError("summary must not be empty")
            updates.append("summary=?"); params.append(summary)
        if description is not None: updates.append("description=?"); params.append(_store_content(description))
        if priority is not None:
            self._validate_choice(priority, {"Highest", "High", "Medium", "Low", "Lowest"}, "priority")
            updates.append("priority=?"); params.append(priority)
        if assignee is not None:
            self._require_user(assignee); self._require_member(issue["project_id"], assignee)
            updates.append("assignee_account_id=?"); params.append(assignee)
        if updates:
            updates.append("updated_at=?"); params.append(self._time()); params.append(issue["id"])
            self.session.execute(f"UPDATE jira_issues SET {','.join(updates)} WHERE id=?", tuple(params))
        if labels is not None:
            self.session.execute("DELETE FROM jira_issue_labels WHERE issue_id=?", (issue["id"],))
            for label in sorted(set(labels), key=str.casefold):
                self.session.execute("INSERT INTO jira_issue_labels(issue_id,label) VALUES(?,?)", (issue["id"], label))
        if transition: return self.transition_issue(str(issue["id"]), transition=transition)
        return self._issue(self._require_issue(str(issue["id"])))

    def list_transitions(self, issue_id_or_key: str) -> dict[str, Any]:
        issue = self._require_issue(issue_id_or_key)
        transitions = {"To Do": (("21", "In Progress"), ("31", "Done")), "In Progress": (("11", "To Do"), ("31", "Done")), "Done": (("11", "To Do"),)}[issue["status"]]
        return {"transitions": [{"id": ident, "name": target, "to": self._status(target)} for ident, target in transitions]}

    def transition_issue(self, issue_id_or_key: str, transition_id: str | None = None, transition: Mapping[str, Any] | None = None, fields: Mapping[str, Any] | None = None, update: Mapping[str, Any] | None = None, properties: list[Mapping[str, Any]] | None = None, history_metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
        transition_id = transition_id or (str(transition.get("id")) if transition else None)
        if transition_id is None: raise JiraValidationError("transition.id is required")
        issue = self._require_issue(issue_id_or_key)
        allowed = {item["id"]: item["name"] for item in self.list_transitions(issue_id_or_key)["transitions"]}
        if transition_id not in allowed:
            raise JiraConflict("transition is not available for the current status")
        target = allowed[transition_id]
        resolution = "Done" if target == "Done" else None
        self.session.execute("UPDATE jira_issues SET status=?,resolution=?,updated_at=? WHERE id=?", (target, resolution, self._time(), issue["id"]))
        return self._issue(self._require_issue(str(issue["id"])))

    def list_comments(self, issue_id_or_key: str, start_at: int = 0, max_results: int = 50) -> dict[str, Any]:
        issue = self._require_issue(issue_id_or_key)
        rows = self.session.execute("SELECT * FROM jira_comments WHERE issue_id=? ORDER BY id", (issue["id"],)).fetchall()
        values = [self._comment(row) for row in rows[start_at:start_at + max_results]]
        return {"startAt": start_at, "maxResults": max_results, "total": len(rows), "comments": values}

    def add_comment(self, issue_id_or_key: str, body: Any, visibility: Mapping[str, Any] | None = None, properties: list[Mapping[str, Any]] | None = None) -> dict[str, Any]:
        issue = self._require_issue(issue_id_or_key); actor = self._require_actor()
        if not _content_text(body).strip(): raise JiraValidationError("comment body must not be empty")
        now = self._time()
        row = self.session.execute("INSERT INTO jira_comments(issue_id,author_account_id,body,created_at,updated_at) VALUES(?,?,?,?,?) RETURNING id", (issue["id"], actor["account_id"], _store_content(body), now, now)).fetchone()
        self.session.execute("UPDATE jira_issues SET updated_at=? WHERE id=?", (now, issue["id"]))
        return self._comment(self.session.execute("SELECT * FROM jira_comments WHERE id=?", (row["id"],)).fetchone())

    def create_issue_link(self, link_type: str | Mapping[str, Any] | None = None, outward_issue: str | Mapping[str, Any] | None = None, inward_issue: str | Mapping[str, Any] | None = None, type: Mapping[str, Any] | None = None, comment: Mapping[str, Any] | None = None) -> dict[str, Any]:
        link_type = _named(type, "name") if type is not None else link_type
        outward_issue = _named(outward_issue, "key", "id")
        inward_issue = _named(inward_issue, "key", "id")
        if not all(isinstance(value, str) for value in (link_type, outward_issue, inward_issue)): raise JiraValidationError("type.name, outwardIssue.key and inwardIssue.key are required")
        outward = self._require_issue(outward_issue); inward = self._require_issue(inward_issue)
        self._validate_choice(link_type, {"Blocks", "Clones", "Duplicate", "Relates"}, "link_type")
        if outward["id"] == inward["id"]: raise JiraValidationError("an issue cannot link to itself")
        existing = self.session.execute("SELECT * FROM jira_issue_links WHERE link_type=? AND outward_issue_id=? AND inward_issue_id=?", (link_type, outward["id"], inward["id"])).fetchone()
        if existing: return self._link(existing)
        row = self.session.execute("INSERT INTO jira_issue_links(link_type,outward_issue_id,inward_issue_id,created_at) VALUES(?,?,?,?) RETURNING id", (link_type, outward["id"], inward["id"], self._time())).fetchone()
        return self._link(self.session.execute("SELECT * FROM jira_issue_links WHERE id=?", (row["id"],)).fetchone())

    def list_boards(self, project: str | None = None, project_key_or_id: str | None = None, start_at: int = 0, max_results: int = 50, board_type: str | None = None, name: str | None = None) -> dict[str, Any]:
        sql = "SELECT b.*,p.key project_key FROM jira_boards b JOIN jira_projects p ON p.id=b.project_id"
        filters: list[str] = []; values: list[Any] = []
        project = project_key_or_id or project
        if project: filters.append("(lower(p.key)=lower(?) OR CAST(p.id AS TEXT)=?)"); values.extend((project, project))
        if board_type: filters.append("b.board_type=?"); values.append(board_type)
        if name: filters.append("lower(b.name) LIKE ?"); values.append(f"%{name.casefold()}%")
        if filters: sql += " WHERE " + " AND ".join(filters)
        params = tuple(values)
        rows = self.session.execute(sql + " ORDER BY b.id", params).fetchall()
        page = rows[start_at:start_at + max_results]
        return {"maxResults": max_results, "startAt": start_at, "total": len(rows), "isLast": start_at + len(page) >= len(rows), "values": [self._board(row) for row in page]}

    def list_sprints(self, board_id: int, start_at: int = 0, max_results: int = 50, state: str | None = None) -> dict[str, Any]:
        if self.session.execute("SELECT id FROM jira_boards WHERE id=?", (board_id,)).fetchone() is None: raise JiraNotFound("board not found")
        sql = "SELECT * FROM jira_sprints WHERE board_id=?"; params: list[Any] = [board_id]
        if state:
            states = [item.strip() for item in state.split(",")]
            sql += " AND state IN (" + ",".join("?" for _ in states) + ")"; params.extend(states)
        rows = self.session.execute(sql + " ORDER BY id", tuple(params)).fetchall()
        page = rows[start_at:start_at + max_results]
        return {"maxResults": max_results, "startAt": start_at, "isLast": start_at + len(page) >= len(rows), "values": [self._sprint(row) for row in page]}

    def create_sprint(self, board_id: int, name: str, goal: str | None = None, start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
        if self.session.execute("SELECT id FROM jira_boards WHERE id=?", (board_id,)).fetchone() is None: raise JiraNotFound("board not found")
        row = self.session.execute("INSERT INTO jira_sprints(board_id,name,state,goal,start_date,end_date,complete_date,created_at) VALUES(?,?, 'future', ?,?,?,NULL,?) RETURNING id", (board_id, name, goal, start_date, end_date, self._time())).fetchone()
        return self._sprint(self.session.execute("SELECT * FROM jira_sprints WHERE id=?", (row["id"],)).fetchone())

    def get_sprint(self, sprint_id: int) -> dict[str, Any]:
        row = self.session.execute("SELECT * FROM jira_sprints WHERE id=?", (sprint_id,)).fetchone()
        if row is None: raise JiraNotFound("sprint not found")
        return self._sprint(row)

    def update_sprint(self, sprint_id: int, state: str | None = None, name: str | None = None, goal: str | None = None, start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
        sprint = self.session.execute("SELECT * FROM jira_sprints WHERE id=?", (sprint_id,)).fetchone()
        if sprint is None: raise JiraNotFound("sprint not found")
        updates: list[str] = []; params: list[Any] = []
        if state is not None:
            allowed = {"future": {"active"}, "active": {"closed"}, "closed": set()}[sprint["state"]]
            if state not in allowed: raise JiraConflict("invalid sprint state transition")
            updates.append("state=?"); params.append(state)
            if state == "closed": updates.append("complete_date=?"); params.append(self._time())
        if name is not None: updates.append("name=?"); params.append(name)
        if goal is not None: updates.append("goal=?"); params.append(goal)
        if start_date is not None: updates.append("start_date=?"); params.append(start_date)
        if end_date is not None: updates.append("end_date=?"); params.append(end_date)
        if updates: params.append(sprint_id); self.session.execute(f"UPDATE jira_sprints SET {','.join(updates)} WHERE id=?", tuple(params))
        return self._sprint(self.session.execute("SELECT * FROM jira_sprints WHERE id=?", (sprint_id,)).fetchone())

    def list_sprint_issues(self, sprint_id: int, start_at: int = 0, max_results: int = 50, jql: str | None = None, fields: list[str] | None = None) -> dict[str, Any]:
        if self.session.execute("SELECT id FROM jira_sprints WHERE id=?", (sprint_id,)).fetchone() is None: raise JiraNotFound("sprint not found")
        rows = self.session.execute("SELECT i.* FROM jira_sprint_issues si JOIN jira_issues i ON i.id=si.issue_id WHERE si.sprint_id=? ORDER BY i.id", (sprint_id,)).fetchall()
        page = rows[start_at:start_at + max_results]
        return {"startAt": start_at, "maxResults": max_results, "total": len(rows), "issues": [self._issue(row) for row in page]}

    def move_issues_to_sprint(self, sprint_id: int, issues: list[str], rank_after_issue: str | None = None, rank_before_issue: str | None = None, rank_custom_field_id: int | None = None) -> dict[str, Any]:
        if self.session.execute("SELECT id FROM jira_sprints WHERE id=?", (sprint_id,)).fetchone() is None: raise JiraNotFound("sprint not found")
        moved = []
        for key in issues:
            issue = self._require_issue(key)
            self.session.execute("DELETE FROM jira_sprint_issues WHERE issue_id=?", (issue["id"],))
            self.session.execute("INSERT INTO jira_sprint_issues(sprint_id,issue_id) VALUES(?,?)", (sprint_id, issue["id"]))
            moved.append(issue["issue_key"])
        return {"sprintId": sprint_id, "issues": moved}

    def _issue(self, row: Mapping[str, Any]) -> dict[str, Any]:
        labels = [item["label"] for item in self.session.execute("SELECT label FROM jira_issue_labels WHERE issue_id=? ORDER BY lower(label)", (row["id"],)).fetchall()]
        return {"id": str(row["id"]), "key": row["issue_key"], "self": f"/rest/api/3/issue/{row['id']}", "fields": {"summary": row["summary"], "description": _load_content(row["description"]), "issuetype": {"name": row["issue_type"]}, "status": self._status(row["status"]), "priority": {"name": row["priority"]}, "reporter": self._user(self._require_user(row["reporter_account_id"])), "assignee": self._user(self._require_user(row["assignee_account_id"])) if row["assignee_account_id"] else None, "labels": labels, "resolution": {"name": row["resolution"]} if row["resolution"] else None, "created": self._json_time(row["created_at"]), "updated": self._json_time(row["updated_at"])}}

    def _comment(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {"id": str(row["id"]), "self": f"/rest/api/3/issue/comment/{row['id']}", "author": self._user(self._require_user(row["author_account_id"])), "body": _load_content(row["body"]), "created": self._json_time(row["created_at"]), "updated": self._json_time(row["updated_at"])}

    def _link(self, row: Mapping[str, Any]) -> dict[str, Any]:
        outward = self._require_issue(str(row["outward_issue_id"])); inward = self._require_issue(str(row["inward_issue_id"]))
        return {"id": str(row["id"]), "type": {"name": row["link_type"]}, "outwardIssue": {"id": str(outward["id"]), "key": outward["issue_key"]}, "inwardIssue": {"id": str(inward["id"]), "key": inward["issue_key"]}}

    @staticmethod
    def _status(name: str) -> dict[str, Any]:
        category = "done" if name == "Done" else ("indeterminate" if name == "In Progress" else "new")
        return {"name": name, "statusCategory": {"key": category}}

    @staticmethod
    def _user(row: Mapping[str, Any]) -> dict[str, Any]:
        return {"accountId": row["account_id"], "displayName": row["display_name"], "emailAddress": row["email"], "active": bool(row["active"])}

    @staticmethod
    def _project(row: Mapping[str, Any]) -> dict[str, Any]:
        return {"id": str(row["id"]), "key": row["key"], "name": row["name"], "description": row["description"], "lead": {"accountId": row["lead_account_id"]}}

    @staticmethod
    def _board(row: Mapping[str, Any]) -> dict[str, Any]:
        return {"id": row["id"], "name": row["name"], "type": row["board_type"], "location": {"projectId": row["project_id"], "projectKey": row["project_key"]}}

    @staticmethod
    def _sprint(row: Mapping[str, Any]) -> dict[str, Any]:
        return {"id": row["id"], "self": f"/rest/agile/1.0/sprint/{row['id']}", "state": row["state"], "name": row["name"], "goal": row["goal"], "originBoardId": row["board_id"], "startDate": JiraOperations._json_time(row["start_date"]), "endDate": JiraOperations._json_time(row["end_date"]), "completeDate": JiraOperations._json_time(row["complete_date"])}

    def _require_actor(self) -> Mapping[str, Any]:
        if self._actor is None: self._actor = self._require_user(self.actor_account_id)
        return self._actor

    def _require_user(self, account_id: str) -> Mapping[str, Any]:
        row = self.session.execute("SELECT * FROM jira_users WHERE account_id=?", (account_id,)).fetchone()
        if row is None: raise JiraNotFound("user not found")
        return row

    def _require_project(self, value: str) -> Mapping[str, Any]:
        row = self.session.execute("SELECT * FROM jira_projects WHERE lower(key)=lower(?) OR CAST(id AS TEXT)=?", (value, value)).fetchone()
        if row is None: raise JiraNotFound("project not found")
        return row

    def _require_issue(self, value: str) -> Mapping[str, Any]:
        row = self.session.execute("SELECT * FROM jira_issues WHERE lower(issue_key)=lower(?) OR CAST(id AS TEXT)=?", (value, value)).fetchone()
        if row is None: raise JiraNotFound("issue not found")
        return row

    def _require_member(self, project_id: int, account_id: str) -> None:
        if self.session.execute("SELECT 1 ok FROM jira_project_members WHERE project_id=? AND account_id=?", (project_id, account_id)).fetchone() is None: raise JiraForbidden("user is not a project member")

    @staticmethod
    def _validate_choice(value: str, choices: set[str], field: str) -> None:
        if value not in choices: raise JiraValidationError(f"{field} must be one of: {', '.join(sorted(choices))}")

    def _time(self) -> Any:
        return self._json_time(self.now())

    @staticmethod
    def _json_time(value: Any) -> Any:
        return value.isoformat() if hasattr(value, "isoformat") else value


def _named(value: Any, *keys: str) -> str | None:
    if value is None: return None
    if isinstance(value, str): return value
    if isinstance(value, Mapping):
        for key in keys:
            candidate = value.get(key)
            if candidate is not None: return str(candidate)
    return None


def _catalog_name(value: Any, catalog: list[dict[str, Any]], field: str) -> str:
    candidate = _named(value, "name", "id")
    if candidate is None: raise JiraValidationError(f"fields.{field}.name or id is required")
    for item in catalog:
        if candidate.casefold() in {str(item["id"]).casefold(), item["name"].casefold()}:
            return item["name"]
    raise JiraValidationError(f"unknown {field}: {candidate}")


def _store_content(value: Any) -> str | None:
    if value is None: return None
    if isinstance(value, str): return value
    return "\x1ejson:" + json.dumps(value, sort_keys=True, separators=(",", ":"))


def _load_content(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("\x1ejson:"):
        return json.loads(value.removeprefix("\x1ejson:"))
    return value


def _content_text(value: Any) -> str:
    if isinstance(value, str): return value
    if isinstance(value, Mapping):
        return " ".join(part for item in value.values() if (part := _content_text(item)))
    if isinstance(value, list):
        return " ".join(part for item in value if (part := _content_text(item)))
    return ""


def _jql_value(jql: str, field: str) -> str | None:
    match = re.search(rf"(?i)(?:^|\s+and\s+){re.escape(field)}\s*=\s*(\"[^\"]+\"|'[^']+'|[^\s]+)", jql)
    return match.group(1).strip("\"'") if match else None
